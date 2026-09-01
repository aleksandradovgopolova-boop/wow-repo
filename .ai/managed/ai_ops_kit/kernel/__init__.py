# kernel — порты и контракты ядра AI Ops.
#
# Ядро зависит ТОЛЬКО от Protocol'ов здесь и TypedDict в shared/contracts.py.
# Реализации (providers/, context/, gates/, delivery/, governance/) внедряются
# на входе транзакции (ai_ops_run), не импортируются в глубине.
