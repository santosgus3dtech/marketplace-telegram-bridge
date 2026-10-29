# Estado atual — Marketplace Telegram Bridge

Revisão documental: **13/10/2026**, America/Sao_Paulo. Git inicial limpo em `main`.

## Evidências

`pyproject.toml` declara Python >=3.12, FastAPI, SQLAlchemy assíncrono, SQLite/aiosqlite, Alembic, HTTPX, cryptography e pytest/Ruff. A árvore separa `app/api`, clientes, banco, schemas e serviços; há testes para OAuth, delivery, retenção, webhooks, hardening e replies.

O README e [arquitetura](ARCHITECTURE.md) descrevem persistência de mensagem/job na mesma transação, outbox durável, deduplicação, locks, backoff e dead-letter. A [segurança](SECURITY.md) especifica allowlists Telegram, correlação de Reply, proteção OAuth e retenção. São especificações/documentação anterior, sem revalidação integral nesta sessão.

`app/services/reply_rules.py` foi lido: normaliza texto, seleciona somente regras cujas palavras-chave configuradas coincidam, respeita janela opcional de horário, ordena por prioridade/ID e retorna sugestão. Não envia mensagens; sua docstring exige decisão humana/política antes de envio.

## Operação e verificações

O README registra `uvicorn app.main:app --reload`, `alembic upgrade head`, `ruff check .`, `ruff format --check .` e `pytest -q`, além de Compose e scripts operacionais. Todos: **não executado nesta tarefa**. Migrações/registro de webhook não foram realizados.

Nenhum banco, histórico de mensagens, ambiente ou credencial foi lido. O uso de API oficial em produção depende de integração aprovada, conforme README; não foi acessado o provider.

## Retomada e lacunas

Última etapa comprovada nesta sessão: índice/estado e instruções de manutenção conferidos. Não foi confirmado serviço 24/7, autorização real, entrega de mensagens ou resultados de testes.

Sugestões sem aprovação presumida:

1. Definir a próxima tarefa nesta edição sanitizada e preservar os contratos existentes.
2. Quando autorizado, validar mecanismos com mocks/fixtures e registrar resultados efetivos.
3. Conciliar referências de rotação OAuth: o README menciona o tema, enquanto arquitetura alerta para não inventar refresh_token não documentado.

Não foram criados roadmap, decisões ou aprendizados artificiais. [Índice](INDEX.md).
