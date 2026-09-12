# Regras e Diretrizes do Projeto Moodle Bot

## 1. Controle de Versão e Git (CRÍTICO)
- **NUNCA execute `git commit` ou `git push` automaticamente.**
- O agente deve realizar as alterações nos arquivos e mantê-las na árvore de trabalho para revisão do usuário.
- Apenas execute `git commit` ou `git push` quando o usuário solicitar **expressamente** na mensagem (ex: "faça o commit", "envie para o github").

## 2. Performance e Exclusão de Dependências
- **NUNCA pesquise, liste ou indexe a pasta `.venv/`, `venv/` ou `node_modules/`.**
- O ambiente virtual contém milhares de pacotes e arquivos de terceiros que deixam o agente lento e consomem contexto desnecessário.
- Todas as operações de busca e leitura de código devem ser estritamente focadas nas pastas de desenvolvimento do projeto:
  - `src/`
  - `config/`
  - `tests/`
- Arquivos de armazenamento em tempo de execução (`storage/cookies/`, `storage/submissions/`, `storage/materials/`) e caches (`__pycache__/`, `.pytest_cache/`) nunca devem ser lidos em lote.

## 3. Testes e Validação
- Ao rodar testes, utilize sempre escopo explícito da pasta de testes (`.venv\Scripts\pytest tests`), evitando varreduras automáticas em scripts temporários ou de rascunho (`scratch/`).
