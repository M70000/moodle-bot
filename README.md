# Moodle AI Assistant (UFMG) 🎓🤖

Assistente em segundo plano (*background daemon*) para o Moodle da UFMG que monitora atividades, baixa materiais de estudo, gera rascunhos de resolução com Google Gemini e solicita aprovação humana via Discord antes do envio.

---

## 📌 Arquitetura e Diretrizes

- **Plataforma & Autenticação:** Moodle UFMG (`moodle.grad.ufmg.br` ou `virtual.ufmg.br`) com SSO MinhaUFMG via Playwright. Os cookies de sessão são armazenados em `storage/cookies/session.json`.
- **Interface & Notificações:** Bot do Discord (`discord.py`) com botões interativos (`[✅ Aprovar e Enviar]`, `[⏱️ Adiar]`, `[❌ Cancelar / Não Enviar]`).
- **Contexto da Disciplina (RAG / Materiais):** Materiais, slides e apostilas postados pelos professores são baixados em `storage/materials/<disciplina>/` e injetados no contexto do Google Gemini (`gemini-2.5-flash`).
- **Submissão Real no Moodle com Revisão Humana Obrigatória:** Submissão automatizada via Playwright com trava estrita de segurança contra sobrescrita (aborta se detectar "Editar envio" para nunca sobrescrever trabalhos enviados manualmente).
- **Conversão Automática para PDF Acadêmico:** Gera PDFs diagramados padrão A4 com cabeçalho da UFMG, Escola de Engenharia, metadados, formatação de tabelas, código e paginação.
- **Hierarquia de IA com Fallback Triplo:**
  - 1º Modelo Oficial: `gemini-3.8-flash`
  - 2º Fallback Secundário: `gemini-3.7-flash`
  - 3º Fallback Terciário: `gemini-3.5-flash-lite`
- **Slash Commands no Discord (Mobile Friendly):**
  - `/tarefas`: Painel de tarefas ativas, prazos restantes e entregues.
  - `/materiais <disciplina>`: Envia slides e apostilas da disciplina no chat.
  - `/resolver <tarefa> [modo] [instruções] [arquivo]`: Resolve tarefas sob demanda com 3 níveis de autonomia (`Apenas Resolver`, `Resolver e Preencher`, `Resolver e Enviar Tudo`).
  - `/resolver_lote [disciplina] [instruções] [arquivo] [material_1..3]`: Menu interativo multi-select para selecionar e resolver múltiplas tarefas/questionários em lote de forma sequencial, com suporte a materiais de referência salvos, upload de arquivos e instruções personalizadas aplicadas a todo o lote (inclusive via botão `[✏️ Instruções]`).
  - `/refazer <tarefa>`: Refaz atividades já entregues com novas instruções.
  - `/status`: Telemetria da sessão Moodle, arquivos catalogados e status dos modelos de IA.
  - `/adicionarconteudo <disciplina> <arquivo>`: Ingestão de novos arquivos para a memória do bot.
  - `/notion_sync` e `/atualizar_checklist`: Sincronização e checklist de rotina diária no Notion.
- **Monitor de Notas e Feedbacks do Professor:** Detecta correções e lança alertas festivos com nota, feedback textual e nome do avaliador.
- **Segurança & Ética (Restrições Estritas):**
  - 🚫 **PROIBIDO** postar em fóruns de dúvidas.
  - 🚫 **PROIBIDO** mandar mensagens diretas para professores ou colegas.
  - 🔒 Todas as credenciais e chaves residem estritamente em `.env` (nunca versionadas).

---

## 📁 Estrutura de Diretórios

```text
c:\moodle-bot\
├── config/                  # Módulo de configurações e leitura de .env
│   ├── __init__.py
│   └── settings.py
├── src/
│   ├── auth/                # Autenticação Playwright (interativa & headless)
│   │   ├── __init__.py
│   │   └── moodle_auth.py
│   ├── scraper/             # Varredura de matérias, prazos, downloads e submissão
│   │   ├── __init__.py
│   │   ├── moodle_scraper.py
│   │   └── moodle_submitter.py
│   ├── solver/              # Resolução de atividades com Gemini IA e PDF
│   │   ├── __init__.py
│   │   ├── gemini_solver.py
│   │   └── pdf_generator.py
│   ├── notifier/            # Bot Discord com Slash Commands e botões interativos
│   │   ├── __init__.py
│   │   └── discord_bot.py
│   └── scheduler/           # Daemon, monitor de prazos (15m, 5m, 2m, 1m) e notas
│       ├── __init__.py
│       ├── daemon.py
│       └── state.py
├── storage/
│   ├── cookies/             # Arquivo session.json (cookies de sessão)
│   ├── materials/           # Slides e PDFs das disciplinas
│   ├── submissions/         # Rascunhos e PDFs acadêmicos gerados
│   └── state.json           # Estado persistente do daemon e tarefas
├── .env.example             # Modelo de configuração de ambiente
├── .gitignore               # Regras de exclusão de segredos e temporários
├── requirements.txt         # Dependências do projeto
└── README.md
```

---

## 🚀 Instalação e Configuração

### 1. Interface Gráfica de Configuração (Recomendado) 🖥️
Você pode configurar todo o `.env` de forma visual e testar suas conexões (Moodle, Gemini, Discord) com apenas 2 cliques:
- **No Windows:** Dê um duplo clique no arquivo [`configurar.bat`](file:///c:/moodle-bot/configurar.bat) na raiz do projeto.
- **Ou via Terminal:**
  ```bash
  .venv\Scripts\python config_gui.py
  ```
Uma janela dedicada será aberta permitindo:
- Configurar URL do Moodle e disparar o login SSO MinhaUFMG com 1 clique.
- Inserir e testar a chave do Google Gemini (com verificação imediata de cota e modelo).
- Inserir e testar o token e canais do bot do Discord.
- Ajustar frequência de varredura e gerenciar pastas de materiais e rascunhos.

---

### 2. Configuração Manual via `.env` (Alternativa)
Se preferir editar manualmente, copie o `.env.example` para `.env` e preencha:
```ini
MOODLE_BASE_URL=https://virtual.ufmg.br
DISCORD_BOT_TOKEN=seu_bot_token
DISCORD_CHANNEL_ID=seu_canal_de_alertas
DISCORD_CONTENT_CHANNEL_ID=0  # Ou ID do canal exclusivo para /adicionarconteudo
GEMINI_API_KEY=sua_chave_do_google_ai_studio
GEMINI_MODEL=gemini-3.8-flash
GEMINI_FALLBACK_MODEL_1=gemini-3.7-flash
GEMINI_FALLBACK_MODEL_2=gemini-3.5-flash-lite
```

### 3. Autenticação Inicial no Moodle / MinhaUFMG
Pode ser disparada direto pelo botão na Interface Gráfica ou via terminal:
```bash
.venv\Scripts\python -m src.auth.moodle_auth
```
- Uma janela gráfica do Chromium será aberta em `https://sistemas.ufmg.br/idp/login.jsp`.
- Digite seu usuário, senha e conclua o 2FA.
- A sessão autenticada é salva em `storage/cookies/session.json` e as próximas execuções ocorrem 100% em segundo plano (*headless*).

### 4. Execução do Assistente Completo (Daemon + Discord Bot)
Para manter o monitoramento contínuo e os Slash Commands ativos:
```bash
.venv\Scripts\python -m src.scheduler.daemon
```

Para rodar apenas uma verificação pontual e sair:
```bash
.venv\Scripts\python -m src.scheduler.daemon --once
```
