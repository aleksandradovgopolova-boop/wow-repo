/**
 * Provider adapters for the WowRepo runner.
 *
 * The pipeline is model-agnostic, so a provider is just "something that can hold
 * a chat and return text". Moonshot (Kimi) exposes an OpenAI-compatible API, so
 * a single openai-compatible adapter covers Moonshot, OpenAI, and other
 * compatible endpoints — the model is a swappable part, exactly as the pipeline
 * contract intends.
 */

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant';
  content: string;
}

export interface ChatProvider {
  readonly name: string;
  chat(messages: ChatMessage[], opts?: { json?: boolean }): Promise<string>;
}

/** Any OpenAI-compatible `/chat/completions` endpoint (Moonshot, OpenAI, …). */
export function openAICompatibleProvider(cfg: {
  name: string;
  baseURL: string;
  apiKey: string;
  model: string;
}): ChatProvider {
  return {
    name: `${cfg.name} (${cfg.model})`,
    async chat(messages, opts) {
      const res = await fetch(`${cfg.baseURL}/chat/completions`, {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          authorization: `Bearer ${cfg.apiKey}`,
        },
        body: JSON.stringify({
          model: cfg.model,
          messages,
          temperature: 0.3,
          ...(opts?.json ? { response_format: { type: 'json_object' } } : {}),
        }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(
          `${cfg.name} API error ${res.status}: ${text.slice(0, 600)}`,
        );
      }
      const data = (await res.json()) as {
        choices?: { message?: { content?: string } }[];
      };
      const content = data.choices?.[0]?.message?.content;
      if (typeof content !== 'string') {
        throw new Error(`${cfg.name}: no message content in response`);
      }
      return content;
    },
  };
}

/** Moonshot (Kimi). Configure with env; base URL and model are overridable. */
export function moonshotProvider(): ChatProvider {
  const apiKey = process.env.MOONSHOT_API_KEY;
  if (!apiKey) {
    throw new Error(
      'MOONSHOT_API_KEY is not set. Export it, e.g.\n' +
        '  export MOONSHOT_API_KEY=sk-...\n' +
        'Optional: MOONSHOT_BASE_URL (default https://api.moonshot.ai/v1), ' +
        'MOONSHOT_MODEL (default kimi-k2-0711-preview).',
    );
  }
  return openAICompatibleProvider({
    name: 'moonshot',
    baseURL: process.env.MOONSHOT_BASE_URL ?? 'https://api.moonshot.ai/v1',
    apiKey,
    model: process.env.MOONSHOT_MODEL ?? 'kimi-k2-0711-preview',
  });
}

/** OpenAI (same adapter, different endpoint) — proof the runner is neutral. */
export function openAIProvider(): ChatProvider {
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) throw new Error('OPENAI_API_KEY is not set.');
  return openAICompatibleProvider({
    name: 'openai',
    baseURL: process.env.OPENAI_BASE_URL ?? 'https://api.openai.com/v1',
    apiKey,
    model: process.env.OPENAI_MODEL ?? 'gpt-4o',
  });
}

/**
 * Offline provider for testing the runner without a key or network. It returns
 * a content analysis, then an INVALID plan on its first plan attempt and a VALID
 * one on the next — so the validate-and-repair loop is genuinely exercised.
 */
export function mockProvider(): ChatProvider {
  let planAttempts = 0;
  return {
    name: 'mock',
    async chat(messages) {
      const last = messages[messages.length - 1]?.content ?? '';
      // Only stage 2's system prompt carries the schema instruction the runner
      // appends, so this reliably distinguishes the plan stage from analysis.
      const isPlanStage = /validates against this JSON Schema/i.test(
        messages.find((m) => m.role === 'system')?.content ?? '',
      );
      if (!isPlanStage) {
        return 'Analysis (mock): a calm, low-density page; primary message is that a place is yours and asks nothing.';
      }
      planAttempts += 1;
      if (planAttempts === 1) {
        // Missing `narrative` and `sections` on purpose → fails validation.
        return JSON.stringify({
          version: 1,
          page: { id: 'mock-place', title: 'Place' },
        });
      }
      // A minimal but VALID plan (zod fills motion/accessibility/order/etc.).
      void last;
      return JSON.stringify({
        version: 1,
        page: {
          id: 'mock-place',
          type: 'place-environment',
          path: '/mock/place',
          title: 'Place',
          purpose: 'demonstrate-the-runner',
          audience: 'a-tester',
          primary_message: 'A place is yours and asks nothing of you.',
          summary: 'A mock page produced by the offline provider.',
          density: 'low',
          tone: ['calm', 'spacious'],
        },
        narrative: ['arrive', 'what-a-place-is', 'onward'],
        sections: [
          { component: 'editorial-hero', source: 'hero', emphasis: 'medium' },
          { component: 'longform-prose', source: 'body' },
        ],
        relationships: {},
      });
    },
  };
}

export function selectProvider(name: string): ChatProvider {
  switch (name) {
    case 'moonshot':
    case 'kimi':
      return moonshotProvider();
    case 'openai':
      return openAIProvider();
    case 'mock':
      return mockProvider();
    default:
      throw new Error(
        `unknown provider "${name}" (use: moonshot | openai | mock)`,
      );
  }
}
