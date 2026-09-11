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
  - `/perguntar <disciplina> <dúvida> [material]`: Tutor acadêmico para tirar dúvidas conceituais citando expressamente os slides e apostilas do professor.
  - `/flashcards <disciplina> [tópico] [qtd] [material]`: Baralhos de repetição espaçada com carrossel interativo no Discord e exportação de arquivo `.txt` para o Anki.
  - `/quiz <disciplina> [qtd_questoes] [tópico] [material]`: Simulado pré-prova interativo com botões A, B, C, D, correção imediata, explicação de pegadinhas e placar final.
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

## 🚀 Pacote Desktop Multi-Usuário (Zero Friction)

O Moodle Bot foi empacotado para ser distribuído entre colegas e turmas sem fricção técnica, garantindo **privacidade individual absoluta** e **instalação com 1 clique**:

```text
├── instalar.bat     # ⚡ Instalador One-Click (detecta/instala Python 3.11, venv, dependências, Chromium e abre o GUI)
├── iniciar.bat      # 🚀 Inicializador Diário com Auto-Updater (busca novidades no git sem tocar no .env e roda o daemon)
├── atualizar.bat    # 🔄 Atualizador Manual Dedicado (git pull + sync de bibliotecas)
└── configurar.bat   # 🖥️ Painel Gráfico de Configurações e Auto-Detecção
```

---

### 1. Instalação One-Click (`instalar.bat`) ⚡
Para novos usuários e colegas:
1. Baixe ou clone o repositório no Windows.
2. Dê um duplo clique no arquivo [`instalar.bat`](file:///c:/moodle-bot/instalar.bat).
3. O script realiza autonomamente:
   - Verificação do Python 3.10+ (com download/instalação silenciosa via `winget` ou setup oficial se necessário).
   - Criação do ambiente virtual isolado `.venv`.
   - Instalação de todas as dependências do `requirements.txt`.
   - Download do navegador Playwright (`playwright install chromium`).
   - Geração do arquivo `.env` inicial.
   - Abertura imediata da Interface Gráfica de Configuração ([`configurar.bat`](file:///c:/moodle-bot/configurar.bat)).

---

### 2. Painel Gráfico & Auto-Detecção de Salas Privadas 🖥️
Na interface gráfica:
- **Bring Your Own Key (BYOK) do Gemini:** Instruções em 3 passos com botão direto para obter a chave gratuita no Google AI Studio.
- **Auto-Detecção do Discord com 1 Clique:**
  - Em vez de copiar e colar 5 IDs de canais manualmente, basta digitar seu nome ou ID do Discord no campo **"Seu Usuário / ID do Discord"** e clicar em **"🔍 Auto-Detectar"**.
  - O sistema localiza (ou provisiona instantaneamente) sua categoria e canais privados no servidor compartilhado e preenche automaticamente todos os 5 campos:
    - 🔔 Alertas & Revisões
    - 📚 Conteúdos & Materiais
    - 📢 Avisos da Turma
    - 📥 Fila Central de Tarefas
    - 🎯 Estudos & Simulados
- **Login Moodle:** 1 clique no botão "Testar Conexão / Fazer Login" para autenticar via SSO MinhaUFMG.

---

### 3. Servidor Discord Compartilhado & Salas Privadas (`🔒 Moodle • Nome`)
Os estudantes podem usar o **mesmo bot e o mesmo servidor do Discord** com sigilo total:
- **Ao entrar no servidor (ou digitar `/meuscanais` / `!meuscanais`):** O bot cria uma categoria privada e os 5 canais exclusivos para aquele estudante.
- **Permissões Estritas:** O cargo `@everyone` tem a visualização bloqueada (`view_channel=False`). Apenas o estudante e o bot têm acesso à categoria e aos canais.
- **Mensagem Inaugural:** Ao criar as salas, o bot posta uma mensagem informativa com instruções de uso e atalhos rápidos.

---

### 4. Execução Diária & Auto-Updater Seguro (`iniciar.bat`) 🔄
Basta dar duplo clique em [`iniciar.bat`](file:///c:/moodle-bot/iniciar.bat):
- O script checa atualizações remotas via `git fetch/pull` de forma transparente.
- **Garantia de Segurança:** Arquivos locais de segredos e credenciais (`.env`, `storage/cookies/session.json`, `storage/state.json`) **nunca são sobrescritos**.
- Inicia o daemon do assistente e conecta os canais do usuário.

---

### 5. Slash Commands Disponíveis no Discord
- `/tarefas`: Exibe painel com todas as atividades abertas e prazos.
- `/resolver <tarefa> [modo] [instruções] [arquivo]`: Resolve tarefas individualmente com IA e gera PDF.
- `/resolver_lote`: Menu interativo multi-select para resolver tarefas em lote sequencial.
- `/meuscanais`: Cria ou localiza suas 5 salas privadas no servidor do Discord.
- `/perguntar <disciplina> <dúvida>`: Tutor acadêmico com citação direta dos slides do professor.
- `/flashcards <disciplina> [tópico]`: Baralhos de repetição espaçada e exportação Anki.
- `/quiz <disciplina>`: Simulado de múltipla escolha pré-prova interativo.
- `/materiais <disciplina>`: Lista e envia materiais didáticos catalogados.
- `/status`: Mostra status do daemon, cookies de sessão e latência da IA.
