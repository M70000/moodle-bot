<div align="center">
  <img src="assets/logo.png" width="120" alt="LumiBot Logo">
  <h1>LumiBot</h1>
  <p><strong>Assistente acadêmico Multi-LMS com suporte a Moodle e Canvas LMS, integração com Discord e Notion, e resolução assistida por modelos de linguagem.</strong></p>

  <p>
    <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.10%2B-blue.svg" alt="Python Version"></a>
    <a href="https://discordpy.readthedocs.io/"><img src="https://img.shields.io/badge/Discord.py-v2.3%2B-5865F2.svg" alt="Discord.py"></a>
    <a href="https://playwright.dev/"><img src="https://img.shields.io/badge/Playwright-Chromium-green.svg" alt="Playwright"></a>
    <a href="https://canvas.instructure.com/doc/api/"><img src="https://img.shields.io/badge/Canvas%20API-REST%20v1-orange.svg" alt="Canvas API"></a>
    <a href="https://developers.notion.com/"><img src="https://img.shields.io/badge/Notion%20API-2022--06--28-black.svg" alt="Notion API"></a>
    <a href="#diretrizes-de-seguran%C3%A7a-e-human-in-the-loop"><img src="https://img.shields.io/badge/Controle-Human--in--the--Loop-red.svg" alt="Human-in-the-Loop"></a>
  </p>
</div>

---

## Visão Geral

O **LumiBot** é um serviço em segundo plano projetado para centralizar o acompanhamento acadêmico, o monitoramento de pendências e o suporte aos estudos universitários. O sistema rastreia tarefas e materiais em portais educacionais, organiza cronogramas e checklists no Notion, emite alertas programados no Discord e auxilia na resolução de atividades utilizando materiais da própria disciplina e modelos de inteligência artificial.

O projeto possui arquitetura desacoplada, permitindo operar com o **Canvas LMS**, com o **Moodle**, ou em modo híbrido com **ambos simultaneamente**. Toda e qualquer ação de entrega em portais institucionais depende estritamente de aprovação humana por botões interativos no Discord.

---

## Arquitetura Multi-LMS

O sistema normaliza os dados de diferentes plataformas de ensino em um modelo de dados comum:

```
[ Canvas LMS (API REST v1) ] ──┐
                               ├─► [ BaseLMSProvider ] ──► [ Daemon, Discord & Notion ]
[ Moodle (Playwright Engine) ] ──┘
```

- **Canvas LMS (Instructure):** Comunicação direta via API REST oficial v1 autenticada por Bearer Token. As consultas são paginadas e a submissão de atividades segue o protocolo oficial em três fases: solicitação de upload, envio binário multipart/form-data e confirmação de entrega do anexo.
- **Moodle:** Automação de sessão com Playwright (Chromium sem interface visual por padrão). Suporta autenticações com Single Sign-On (SSO) institucional, preservando os cookies de sessão validados ou realizando renovação periódica com usuário e senha.
- **Modo Híbrido (`both`):** Monitora os dois ambientes concorrentemente, consolidando todas as disciplinas e tarefas em uma fila unificada de notificações e alertas.

---

## Requisitos do Sistema

- **Sistema Operacional:** Windows 10/11 ou distribuições Linux x86_64.
- **Python:** Versão 3.10, 3.11 ou 3.12.
- **Aplicação no Discord:** Bot registrado no [Discord Developer Portal](https://discord.com/developers/applications) com permissões de envio de mensagens, anexos e as seguintes *Privileged Gateway Intents* ligadas:
  - `Message Content Intent`
  - `Server Members Intent`
- **Chaves de API de IA:** Pelo menos uma chave configurada (Google Gemini, DeepSeek ou Anthropic Claude).
- **Integração com Notion (Opcional):** Token de integração interna e identificadores de banco de dados/páginas para sincronização de calendário e tarefas.

---

## Instalação e Inicialização Rápida

### Windows

O repositório fornece scripts em lote (`.bat`) para facilitar todas as etapas sem necessidade de comandos complexos no terminal:

1. **Clonar ou Baixar o Projeto:**
   ```cmd
   git clone https://github.com/seu-usuario/moodle-bot.git
   cd moodle-bot
   ```

2. **Instalar Dependências:**
   Dê um duplo clique no arquivo `instalar.bat`. O script criará o ambiente virtual isolado (`.venv`), instalará todas as dependências Python necessárias e efetuará o download dos binários do Chromium utilizados pelo Playwright.

3. **Configurar Serviços e Credenciais:**
   Dê um duplo clique em `configurar.bat`. O painel gráfico local será iniciado e abrirá no seu navegador padrão no endereço `http://127.0.0.1:5055`.

4. **Iniciar o Assistente:**
   Dê um duplo clique em `iniciar.bat`. O assistente validará o arquivo `.env` e executará o daemon de monitoramento e o bot do Discord em segundo plano, disponibilizando o ícone na bandeja do sistema (System Tray).

---

### Servidor Linux

Para hospedar o LumiBot em servidores Linux dedicados ou VPS:

```bash
# 1. Clonar o repositório
git clone https://github.com/seu-usuario/moodle-bot.git
cd moodle-bot

# 2. Criar e ativar o ambiente virtual
python3 -m venv .venv
source .venv/bin/activate

# 3. Instalar dependências e o navegador Chromium
pip install -r requirements.txt
playwright install chromium

# 4. Configurar variáveis de ambiente
cp .env.example .env
nano .env

# 5. Iniciar o serviço
python -m src.scheduler.daemon
```

---

## Configuração Passo a Passo

### 1. Painel Web Integrado (`configurar.bat`)

O painel visual local (`http://127.0.0.1:5055`) permite ajustar parâmetros sem editar arquivos manualmente:

- **Seleção de Provedores:** Escolha entre `Canvas LMS`, `Moodle` ou `Multi-LMS (Ambas)`. As abas de configuração do painel adaptam-se para exibir apenas os campos necessários.
- **Configuração do Discord:** Insira o Token do Bot, o ID do Servidor e os IDs dos canais de notificação. Utilize o botão **Auto-Detectar Meus Canais** caso já tenha executado o comando `/meuscanais`.
- **Modelos de Inteligência Artificial:** Escolha o provedor padrão (Gemini, DeepSeek ou Claude) e defina a ordem de redundância caso um provedor atinja limite de requisições.
- **Notion & Agenda:** Insira o token de integração e os identificadores das tabelas e páginas.
- **Teste de Conectividade:** Botões interativos em cada aba validam credenciais e conectividade antes de salvar.
- **Gravação Segura:** Salva as alterações diretamente no arquivo `.env` preservando comentários e formatação existente.

---

### 2. Configuração do Canvas LMS

1. Acesse o portal Canvas da sua instituição (exemplo: `https://pucminas.instructure.com`).
2. Clique no ícone do seu perfil no menu lateral esquerdo (**Conta**) e entre em **Configurações**.
3. Na seção **Tokens de Acesso Aprovados**, clique no botão **+ Novo Token de Acesso**.
4. Digite uma finalidade (exemplo: `LumiBot`), defina ou não data de expiração e clique em **Gerar Token**.
5. Copie o código alfanumérico gerado imediatamente.
6. No painel de configuração (ou no arquivo `.env`):
   - **URL Base:** `https://pucminas.instructure.com` (ou o domínio do Canvas da sua faculdade).
   - **Token de Acesso:** Cole o token gerado.
7. Clique em **Testar Conexão** para confirmar a autenticação com a API REST.

---

### 3. Configuração do Moodle

1. Informe a **URL Base do Moodle** da sua instituição (exemplo: `https://virtual.ufmg.br`).
2. Selecione a modalidade de autenticação:
   - **Cookies de Sessão (Recomendado para 2FA / SSO):** Clique no botão de login assistido no painel ou execute `/login` no Discord. Uma janela do navegador Chromium será exibida para que você faça o login manualmente. Os cookies de sessão serão salvos de forma encriptada em `storage/cookies/session.json`.
   - **Credenciais Automáticas:** Insira usuário e senha institucionais para que o Playwright renove os cookies em segundo plano sempre que expirarem.
3. Teste a conexão para assegurar o acesso às páginas dos cursos.

---

### 4. Configuração dos Provedores de IA (BYOK)

O sistema utiliza a arquitetura *Bring Your Own Key* (BYOK), conectando-se diretamente às APIs oficiais:

- **Google Gemini (Padrão Recomendado):** Obtenha sua chave gratuita no [Google AI Studio](https://aistudio.google.com/) e configure `GEMINI_API_KEY`. Suporta modelos rápidos e multimodais como `gemini-3.8-flash`, `gemini-3.7-flash` e `gemini-3.5-flash`.
- **DeepSeek:** Compatível com os modelos `deepseek-chat` e `deepseek-flash`, com suporte a raciocínio analítico passo a passo (Chain of Thought / CoT). Configure `DEEPSEEK_API_KEY`.
- **Anthropic Claude:** Compatível com `claude-3-5-sonnet` e `claude-3-5-haiku`. Configure `ANTHROPIC_API_KEY`.

---

## Integração com o Notion

A integração do LumiBot com o Notion utiliza a API oficial (versão `2022-06-28`) para manter seu espaço de estudos sincronizado com as plataformas de ensino.

### Recursos da Integração

- **Sincronização de Atividades (`/notion_sync`):** Varre as pendências do Canvas e Moodle e as insere na tabela de tarefas do Notion com título, data de entrega, disciplina associada, link original e checklist de execução com caixas de seleção (*to-do*).
- **Prevenção de Duplicatas:** Cada item criado recebe uma propriedade interna `Task ID`. Se uma atividade já estiver cadastrada na base do Notion, o registro existente é mantido sem duplicações.
- **Checklist Diária (`/atualizar_checklist`):** Localiza o bloco de tarefas do dia na sua página do Notion, limpa os itens concluídos e reconstrói a lista combinando os blocos de estudo da sua tabela de rotina semanal e as tarefas com entrega prevista para a data.
- **Inclusão Rápida de Itens (`/notion_adicionar`):** Registra notas, revisões, leituras ou pendências avulsas no Notion diretamente pelo Discord.
- **Anúncios no Discord:** Sempre que uma tarefa ou anotação é gravada no Notion, o bot envia uma mensagem formatada com link direto da página no canal de avisos.

### Passo a Passo para Conectar o Notion

1. Acesse o portal [Notion Developers - Integrations](https://www.notion.so/profile/integrations) e clique em **New integration**.
2. Dê um nome à integração (exemplo: `LumiBot`), selecione o seu espaço de trabalho (*workspace*) e conclua a criação.
3. Copie o **Internal Integration Secret** (token que inicia com `ntn_` ou `secret_`).
4. Abra o Notion no navegador ou aplicativo e vá até a página principal onde estão sua central de estudos, tabelas e blocos.
5. Clique no menu de opções (`...`) no canto superior direito da página, selecione **Conexões** (ou *Connect to*) e adicione a integração recém-criada. Repita esse procedimento nas databases de tarefas e cursos se estiverem em páginas separadas.
6. Localize os identificadores necessários a partir das URLs:
   - **ID da Página Principal (`NOTION_PAGE_ID`):** Código alfanumérico de 32 caracteres presente no final da URL da página principal.
   - **ID do Banco de Tarefas (`NOTION_TASKS_DATABASE_ID`):** Abra o banco de dados de tarefas como página inteira e copie a sequência de 32 caracteres contida na URL antes da interrogação `?`.
   - **ID do Banco de Disciplinas (`NOTION_COURSES_DATABASE_ID`):** Código da database que relaciona disciplinas, nomes abreviados e códigos das matérias.
   - **ID do Bloco de Checklist Diária (`NOTION_DAILY_CHECKLIST_BLOCK_ID`):** Clique com o botão direito no bloco de checklist diária na página, selecione *Copy link to block* e copie a sequência final após o `#`.
   - **ID da Tabela de Horários (`NOTION_WEEKLY_SCHEDULE_TABLE_ID`):** ID do bloco de tabela que contém a agenda de estudos semanal de segunda a domingo.
7. Insira essas informações no painel web (`configurar.bat`) ou no arquivo `.env`:
   ```env
   NOTION_API_KEY=ntn_sua_chave_aqui
   NOTION_PAGE_ID=32_caracteres_da_pagina
   NOTION_TASKS_DATABASE_ID=32_caracteres_do_banco_tarefas
   NOTION_COURSES_DATABASE_ID=32_caracteres_do_banco_cursos
   NOTION_DAILY_CHECKLIST_BLOCK_ID=32_caracteres_do_bloco
   NOTION_WEEKLY_SCHEDULE_TABLE_ID=32_caracteres_da_tabela
   ```

---

## Funcionalidades do Sistema

Esta seção descreve todas as capacidades operacionais e módulos integrados no LumiBot:

### 1. Monitoramento Contínuo em Segundo Plano (Daemon)
- Execução assíncrona programada com ciclo de checagem configurável (`CHECK_INTERVAL_MINUTES`).
- Varredura concorrente e silenciosa de tarefas pendentes, novos materiais didáticos e comunicados institucionais.
- Sistema de cache local para evitar notificações repetidas sobre itens já catalogados.
- Integração nativa com a bandeja do sistema do Windows (System Tray), permitindo ocultar a janela do terminal com atualização de status visual e menu de contexto para encerramento ou restauração.

### 2. Sistema de Alertas Escalonados e Contagem Regressiva
- Notificações automáticas no canal `#alertas-e-revisões` para tarefas com aproximação do prazo final.
- Régua de alertas programados em 4 janelas críticas:
  - **24 horas de antecedência:** Alerta inicial com detalhes da atividade e prazo restante.
  - **12 horas de antecedência:** Alerta de prioridade moderada.
  - **2 horas de antecedência:** Alerta urgente destacando a proximidade do encerramento.
  - **30 minutos de antecedência:** Alerta crítico final com botão para resolução imediata.

### 3. Base de Conhecimento Local e RAG Acadêmico
- Download automático de apresentações de slides, apostilas, listas de exercícios e PDFs postados pelos professores para a pasta local `storage/materials/<disciplina>/`.
- Processamento e indexação de texto para viabilizar recuperação aumentada por geração (RAG).
- Quando questionada ou solicitada a resolver uma tarefa, a IA localiza trechos exatos dos materiais oficiais da matéria, formulando respostas fundamentadas e indicando títulos de arquivos e números de páginas.
- Adição manual de conteúdos complementares via comando `/adicionarconteudo`, permitindo incluir resumos pessoais e anotações de aula na base de dados.

### 4. Motor de Resolução Assistida de Atividades
- Análise semântica do enunciado da tarefa ou do questionário, consulta aos materiais da disciplina e geração de solução completa com equações matemáticas (LaTeX), blocos de código e justificativas conceituais.
- Suporte a 3 modos de execução:
  - **Modo 1 (Apenas Resolver):** Gera o documento com a resolução e disponibiliza o arquivo formatado em PDF e DOCX no Discord para revisão.
  - **Modo 2 (Resolver e Preencher):** Efetua o rascunho e insere as respostas diretamente nos campos correspondentes da tarefa no portal, sem realizar o envio final.
  - **Modo 3 (Resolver e Enviar Tudo):** Executa o fluxo completo e envia a atividade após processar e validar o formulário.
- **Geração de Documentos Profissionais:** Produção automática de relatórios em `.docx` e `.pdf` com cabeçalho institucional, tipografia padronizada e formatação limpa pronta para submissão acadêmica.

### 5. Fila de Resolução em Lote (`/resolver_lote`)
- Interface interativa no Discord que lista todas as atividades pendentes detectadas no Moodle ou Canvas.
- Seleção de múltiplos itens via menu *dropdown* para processamento em fila assíncrona.
- Registro contínuo de status no canal `#fila-de-tarefas`, indicando tarefas em andamento, concluídas e pendentes de validação.

### 6. Reprocessamento e Refação (`/refazer`)
- Permite submeter novas instruções, correções de enunciados ou critérios adicionais para tarefas já entregues ou rascunhadas anteriormente.
- A IA reavalia o contexto anterior, ajusta o raciocínio conforme as diretrizes do estudante e gera uma nova versão revisada do arquivo.

### 7. Suíte de Estudos Ativos e Fixação
- **Tutor Acadêmico (`/perguntar`):** Canal de perguntas e respostas conceituais fundamentado nos materiais do curso. A IA esclarece dúvidas citando os documentos da pasta da disciplina.
- **Geração de Flashcards para Anki (`/flashcards`):** Criação de baralhos conceituais de repetição espaçada. O bot disponibiliza os cartões em formato de carrossel interativo no Discord e anexa um arquivo `.txt` formatado para importação direta no Anki.
- **Simulados Pré-Prova (`/quiz`):** Geração de questões de múltipla escolha com botões (A, B, C, D) interativos no chat, contagem de acertos, pontuação e explicação detalhada da alternativa correta.

### 8. Provisionamento de Salas Pessoais Privadas (`/meuscanais`)
- Criação com um único clique de uma categoria exclusiva para o usuário no servidor do Discord, com permissões estritas (apenas o aluno e o bot têm acesso).
- Divisão em 5 canais temáticos dedicados:
  - `#alertas-e-revisões`: Prazos, avisos urgentes e botões de validação e entrega.
  - `#conteúdos`: Repositório de arquivos didáticos e resumos indexados.
  - `#avisos-da-turma`: Feed de comunicados institucionais extraídos dos portais.
  - `#fila-de-tarefas`: Painel de monitoramento de execuções em lote.
  - `#estudos-e-simulados`: Sala para sessões com o tutor, resolução de quizzes e flashcards.

### 9. Mecanismo de Submissão Direta no Canvas e Moodle
- **Submissão no Canvas LMS:** Implementação do protocolo de entrega tripartite da API REST v1. O bot notifica a intenção de upload para a rota `/api/v1/courses/:course_id/assignments/:assignment_id/submissions/self/files`, transmite o arquivo gerado via requisição multipart e finaliza a submissão vinculando o anexo à entrega oficial da atividade.
- **Submissão no Moodle:** Automação de upload de arquivos no gerenciador de rascunhos do Moodle via Playwright Chromium, lidando com confirmação de envio e termos de integridade acadêmica.

---

## Referência Completa de Comandos do Discord

O LumiBot aceita tanto comandos de barra (*slash commands*, `/`) quanto comandos de texto por prefixo (`!`).

| Comando de Barra | Comando por Prefixo | Parâmetros | Descrição Detalhada |
| :--- | :--- | :--- | :--- |
| `/tarefas` | `!tarefas` | `disciplina` *(opcional)* | Consulta a lista unificada de tarefas acadêmicas com filtros por matéria e status de entrega. |
| `/canvas` | `!canvas` | `consulta` *(obrigatório)*, `dias` *(opcional, padrão: 7)* | Consulta dados específicos do Canvas LMS (`tarefas`, `disciplinas`, `avisos` ou `status`). |
| `/status` | `!status` | *(Nenhum)* | Exibe diagnóstico do daemon, estado da sessão do Moodle/Canvas, materiais indexados e status das APIs de IA. |
| `/login` | `!login` | `metodo` *(opcional: "auto" ou "navegador")* | Inicia login interativo no navegador Playwright (com suporte a 2FA) ou renova credenciais silenciosamente. |
| `/materiais` | `!materiais` | `disciplina` *(obrigatório)* | Lista e envia no chat os arquivos didáticos e slides catalogados da disciplina. |
| `/adicionarconteudo` | `!adicionarconteudo` | `disciplina` *(obrigatório)*, `arquivo` *(obrigatório)* | Salva e indexa arquivos, notas e apostilas complementares na base de conhecimento da matéria. |
| `/resolver` | `!resolver` | `tarefa` *(obrigatório)*, `modo` *(opcional)*, `instrucoes` *(opcional)*, `arquivo` *(opcional)* | Resolve uma atividade pendente com base nos materiais didáticos e gera arquivo DOCX/PDF para revisão. |
| `/refazer` | `!refazer` | `tarefa` *(obrigatório)*, `instrucoes` *(opcional)*, `arquivo` *(opcional)* | Refaz uma atividade previamente resolvida aplicando novas instruções, dados ou correções. |
| `/resolver_lote` | `!resolver_lote` (ou `!lote`) | `disciplina` *(opcional)*, `instrucoes` *(opcional)*, `arquivo` *(opcional)* | Abre menu interativo para selecionar e enfileirar múltiplas atividades para resolução sequencial. |
| `/perguntar` | `!perguntar` | `disciplina` *(obrigatório)*, `duvida` *(obrigatório)*, `material` *(opcional)* | Consulta o tutor inteligente para esclarecer dúvidas conceituais citando fontes dos materiais do curso. |
| `/flashcards` | `!flashcards` | `disciplina` *(obrigatório)*, `topico` *(opcional)*, `qtd` *(opcional, padrão: 5)*, `material` *(opcional)* | Gera baralho de memorização com carrossel interativo e exportação de arquivo `.txt` para o Anki. |
| `/quiz` | `!quiz` | `disciplina` *(obrigatório)*, `qtd_questoes` *(opcional, padrão: 5)*, `topico` *(opcional)*, `material` *(opcional)* | Inicia simulado pré-prova interativo com botões A, B, C, D e explicação detalhada de respostas. |
| `/meuscanais` | `!meuscanais` | *(Nenhum)* | Provisiona ou localiza automaticamente sua categoria privada e as 5 salas temáticas no servidor. |
| `/notion_adicionar` | *(Exclusivo `/`)* | `titulo` *(obrigatório)*, `tipo` *(obrigatório)*, `disciplina` *(opcional)*, `prazo` *(opcional)* | Cria tarefa, nota ou estudo na base do Notion (`TAREFA✅`, `ESTUDO📚`, `LISTA📝`, `REVISÃO🔄`, `PROVA🎯`, `OUTROS`). |
| `/notion_sync` | *(Exclusivo `/`)* | *(Nenhum)* | Sincroniza todas as pendências com prazo dos portais educacionais com o banco de dados do Notion. |
| `/atualizar_checklist` | *(Exclusivo `/`)* | *(Nenhum)* | Reconstrói o bloco de checklist diária no Notion com os blocos da rotina semanal e entregas do dia. |
| `/ajuda` | `!ajuda` (ou `!help`) | *(Nenhum)* | Exibe o catálogo completo de comandos, recursos e modos de operação do assistente. |
| `/help` | *(Sinônimo)* | *(Nenhum)* | Alias do comando de ajuda do bot. |

---

## Diretrizes de Segurança e Human-in-the-Loop

> [!IMPORTANT]
> O LumiBot adota a política obrigatória de supervisão humana (*Human-in-the-Loop*). Nenhuma atividade é submetida ao portal educacional sem validação e autorização expressa do usuário.

- **Aprovação Obrigatória por Botões:** Ao concluir a resolução de qualquer atividade, o bot envia o documento gerado em formato DOCX/PDF e exibe botões de ação:
  - `[Aprovar e Enviar]`: Dispara o protocolo oficial de entrega no Canvas ou Moodle.
  - `[Adiar]`: Mantém a tarefa como pendente e emite novo lembrete posteriormente.
  - `[Cancelar]`: Descarta o rascunho gerado sem qualquer alteração no portal de ensino.
- **Proteção Contra Sobrescrita:** Caso o assistente identifique que uma atividade já foi entregue manualmente pelo estudante, qualquer rotina de envio automatizado é bloqueada para evitar a substituição indevida do trabalho original.
- **Isolamento de Credenciais Locais:** Senhas, chaves de API e tokens de autenticação ficam armazenados exclusivamente no computador local (arquivo `.env` e diretório `storage/cookies/`). Nenhuma informação confidencial é compartilhada com servidores intermediários.
- **Restrição de Interação Social:** O bot não possui funcionalidades para enviar mensagens diretas a colegas ou professores, nem publica comentários em fóruns ou murais de turma.
