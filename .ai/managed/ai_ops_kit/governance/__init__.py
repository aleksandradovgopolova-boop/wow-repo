"""governance — исполняемые политики и журнал решений AI (Фаза 4, лента 5).

policy_engine: допустимое поведение по действию (Suggest→Prepare→Execute→Require approval) —
исполняемый gate, а не пожелание. Пакет-лист: зависит только от stdlib+yaml, других пакетов
ai_ops_kit не импортирует (поэтому в layering.yaml как ребро не появляется).
"""
