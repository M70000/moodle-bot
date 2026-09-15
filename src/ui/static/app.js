// ===================================================================
// Moodle AI Assistant - Frontend Logic (Vanilla JS)
// ===================================================================

const tabMetadata = {
  'tab-lms': {
    title: 'Plataformas LMS',
    subtitle: 'Defina a arquitetura de plataformas e configure suas integrações.'
  },
  'tab-canvas': {
    title: 'Canvas LMS',
    subtitle: 'Configure a URL do portal Canvas e o token de acesso da API.'
  },
  'tab-moodle': {
    title: 'Moodle',
    subtitle: 'Configure a URL base do Moodle e a autenticação SSO.'
  },
  'tab-discord': {
    title: 'Discord Bot & Notificações',
    subtitle: 'Configure o token do bot e os canais para recebimento de alertas e revisão.'
  },
  'tab-gemini': {
    title: 'Inteligência Artificial & Fallbacks',
    subtitle: 'Gerencie o provedor principal (Gemini, Claude, DeepSeek) e a cadeia de contingência.'
  },
  'tab-scheduler': {
    title: 'Prazos & Agendamento',
    subtitle: 'Ajuste a frequência de varredura e regras de contagem regressiva.'
  },
  'tab-storage': {
    title: 'Armazenamento & Diretórios',
    subtitle: 'Pastas locais de cookies, materiais didáticos e rascunhos em PDF.'
  },
  'tab-notion': {
    title: 'Notion & Central de Estudos',
    subtitle: 'Sincronize tarefas, lista de exercícios e checklist diário com o Notion.'
  }
};

document.addEventListener('DOMContentLoaded', () => {
  setupNavigation();
  loadConfig();
  loadStatus();

  // Listeners de Ação
  document.getElementById('btn-save-env').addEventListener('click', saveConfig);
  document.getElementById('btn-reload-status').addEventListener('click', () => {
    loadStatus();
    showToast('Status atualizado!', 'info');
  });

  document.getElementById('btn-test-canvas')?.addEventListener('click', testCanvas);
  document.getElementById('btn-test-moodle').addEventListener('click', testMoodle);
  document.getElementById('btn-test-discord').addEventListener('click', testDiscord);
  document.getElementById('btn-test-gemini').addEventListener('click', testGemini);

  // Alternância dinâmica de Arquitetura LMS (Multi vs Canvas vs Moodle)
  document.querySelectorAll('input[name="LMS_PROVIDER"]').forEach(radio => {
    radio.addEventListener('change', (e) => {
      updateLmsTabsVisibility(e.target.value);
    });
  });
  document.getElementById('card-lms-multi')?.addEventListener('click', () => {
    const r = document.getElementById('lms-multi');
    if (r) { r.checked = true; updateLmsTabsVisibility('multi'); }
  });
  document.getElementById('card-lms-canvas')?.addEventListener('click', () => {
    const r = document.getElementById('lms-canvas');
    if (r) { r.checked = true; updateLmsTabsVisibility('canvas'); }
  });
  document.getElementById('card-lms-moodle')?.addEventListener('click', () => {
    const r = document.getElementById('lms-moodle');
    if (r) { r.checked = true; updateLmsTabsVisibility('moodle'); }
  });

  // Toggle de visualização do token do Canvas
  const btnToggleCanvasToken = document.getElementById('btn-toggle-canvas-token');
  if (btnToggleCanvasToken) {
    btnToggleCanvasToken.addEventListener('click', () => {
      const input = document.getElementById('CANVAS_API_TOKEN');
      if (input) {
        if (input.type === 'password') {
          input.type = 'text';
          btnToggleCanvasToken.textContent = '🔒';
        } else {
          input.type = 'password';
          btnToggleCanvasToken.textContent = '👁️';
        }
      }
    });
  }

  // Alternância dinâmica de Modo de Autenticação do Canvas (Token vs Cookies vs Credenciais)
  document.querySelectorAll('input[name="CANVAS_AUTH_MODE"]').forEach(radio => {
    radio.addEventListener('change', (e) => {
      updateCanvasAuthModeUI(e.target.value);
    });
  });
  document.getElementById('card-canvas-mode-token')?.addEventListener('click', () => {
    const r = document.getElementById('canvas-mode-token');
    if (r) { r.checked = true; updateCanvasAuthModeUI('token'); }
  });
  document.getElementById('card-canvas-mode-cookies')?.addEventListener('click', () => {
    const r = document.getElementById('canvas-mode-cookies');
    if (r) { r.checked = true; updateCanvasAuthModeUI('cookies'); }
  });
  document.getElementById('card-canvas-mode-credentials')?.addEventListener('click', () => {
    const r = document.getElementById('canvas-mode-credentials');
    if (r) { r.checked = true; updateCanvasAuthModeUI('credentials'); }
  });

  // Toggle de visualização da senha do Canvas
  const btnToggleCanvasPwd = document.getElementById('btn-toggle-canvas-password');
  if (btnToggleCanvasPwd) {
    btnToggleCanvasPwd.addEventListener('click', () => {
      const pwdInput = document.getElementById('CANVAS_PASSWORD');
      if (pwdInput) {
        if (pwdInput.type === 'password') {
          pwdInput.type = 'text';
          btnToggleCanvasPwd.textContent = '🔒';
        } else {
          pwdInput.type = 'password';
          btnToggleCanvasPwd.textContent = '👁️';
        }
      }
    });
  }

  document.getElementById('btn-trigger-canvas-login')?.addEventListener('click', triggerCanvasLogin);
  document.getElementById('btn-trigger-canvas-cred-login')?.addEventListener('click', testCanvasCredentialsLogin);

  const btnTestClaude = document.getElementById('btn-test-claude');
  if (btnTestClaude) btnTestClaude.addEventListener('click', testClaude);

  const btnTestDeepSeek = document.getElementById('btn-test-deepseek');
  if (btnTestDeepSeek) btnTestDeepSeek.addEventListener('click', testDeepSeek);

  const btnTestNotion = document.getElementById('btn-test-notion');
  if (btnTestNotion) btnTestNotion.addEventListener('click', testNotion);

  document.getElementById('btn-trigger-login').addEventListener('click', triggerLogin);
  document.getElementById('btn-nav-lms')?.addEventListener('click', () => switchTab('tab-lms'));
  document.getElementById('sidebar-moodle-item')?.addEventListener('click', () => switchTab('tab-moodle'));
  document.getElementById('sidebar-canvas-item')?.addEventListener('click', () => switchTab('tab-canvas'));

  // Alternância dinâmica de Modo de Autenticação (Cookies vs Credenciais)
  document.querySelectorAll('input[name="AUTH_MODE"]').forEach(radio => {
    radio.addEventListener('change', (e) => {
      updateAuthModeUI(e.target.value);
    });
  });
  document.getElementById('card-mode-cookies')?.addEventListener('click', () => {
    const r = document.getElementById('mode-cookies');
    if (r) { r.checked = true; updateAuthModeUI('cookies'); }
  });
  document.getElementById('card-mode-credentials')?.addEventListener('click', () => {
    const r = document.getElementById('mode-credentials');
    if (r) { r.checked = true; updateAuthModeUI('credentials'); }
  });

  // Toggle de visualização da senha institucional
  const btnTogglePwd = document.getElementById('btn-toggle-password');
  if (btnTogglePwd) {
    btnTogglePwd.addEventListener('click', () => {
      const pwdInput = document.getElementById('MOODLE_PASSWORD');
      if (pwdInput) {
        if (pwdInput.type === 'password') {
          pwdInput.type = 'text';
          btnTogglePwd.textContent = '🔒';
        } else {
          pwdInput.type = 'password';
          btnTogglePwd.textContent = '👁️';
        }
      }
    });
  }

  // Disparo de teste de login automático por credenciais
  const btnTriggerCredLogin = document.getElementById('btn-trigger-cred-login');
  if (btnTriggerCredLogin) {
    btnTriggerCredLogin.addEventListener('click', testCredentialsLogin);
  }

  const autoStartCheckbox = document.getElementById('AUTO_START_WINDOWS');
  if (autoStartCheckbox) {
    autoStartCheckbox.addEventListener('change', async () => {
      const enabled = autoStartCheckbox.checked;
      try {
        const res = await fetch('/api/startup', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled })
        });
        const data = await res.json();
        if (data.ok) {
          showToast(enabled ? 'Adicionado à inicialização do Windows!' : 'Removido da inicialização do Windows!', 'success');
        } else {
          showToast('Erro ao atualizar inicialização: ' + data.error, 'error');
        }
      } catch (err) {
        showToast('Erro de conexão: ' + err.message, 'error');
      }
    });
  }
});


// Navegação entre Abas
function setupNavigation() {
  const navItems = document.querySelectorAll('.nav-item');
  navItems.forEach(item => {
    item.addEventListener('click', () => {
      const tabId = item.getAttribute('data-tab');
      if (!tabId) return;

      navItems.forEach(n => n.classList.remove('active'));
      item.classList.add('active');

      document.querySelectorAll('.tab-pane').forEach(pane => pane.classList.remove('active'));
      const activePane = document.getElementById(tabId);
      if (activePane) activePane.classList.add('active');

      const meta = tabMetadata[tabId];
      if (meta) {
        document.getElementById('page-title').textContent = meta.title;
        document.getElementById('page-subtitle').textContent = meta.subtitle;
      }
    });
  });
}

// Alterna ativamente para uma aba específica
function switchTab(tabId) {
  const navItem = document.querySelector(`.nav-item[data-tab="${tabId}"]`);
  if (navItem) {
    navItem.click();
  }
}

// Carrega Configuração do Backend
async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    const data = await res.json();
    const config = data.config || {};

    // Canvas LMS & Multi-LMS
    const lmsProvider = (config.LMS_PROVIDER || 'multi').toLowerCase();
    const lmsRadio = document.querySelector(`input[name="LMS_PROVIDER"][value="${lmsProvider}"]`);
    if (lmsRadio) lmsRadio.checked = true;
    updateLmsTabsVisibility(lmsProvider);

    setInputValue('CANVAS_BASE_URL', config.CANVAS_BASE_URL || 'https://pucminas.instructure.com');
    const canvasAuthMode = (config.CANVAS_AUTH_MODE || 'token').toLowerCase();
    const canvasModeRadio = document.querySelector(`input[name="CANVAS_AUTH_MODE"][value="${canvasAuthMode}"]`);
    if (canvasModeRadio) canvasModeRadio.checked = true;
    setInputValue('CANVAS_API_TOKEN', config.CANVAS_API_TOKEN || '');
    setInputValue('CANVAS_USERNAME', config.CANVAS_USERNAME || '');
    setInputValue('CANVAS_PASSWORD', config.CANVAS_PASSWORD || '');
    updateCanvasAuthModeUI(canvasAuthMode);

    setInputValue('MOODLE_BASE_URL', config.MOODLE_BASE_URL || 'https://virtual.ufmg.br');
    const authMode = config.AUTH_MODE || 'cookies';
    const modeRadio = document.querySelector(`input[name="AUTH_MODE"][value="${authMode}"]`);
    if (modeRadio) modeRadio.checked = true;
    setInputValue('MOODLE_USERNAME', config.MOODLE_USERNAME || '');
    setInputValue('MOODLE_PASSWORD', config.MOODLE_PASSWORD || '');
    updateAuthModeUI(authMode);

    setCheckboxValue('HEADLESS_LOGIN', config.HEADLESS_LOGIN === 'true');
    setInputValue('LOGIN_TIMEOUT_SECONDS', config.LOGIN_TIMEOUT_SECONDS || '300');

    setInputValue('DISCORD_BOT_TOKEN', config.DISCORD_BOT_TOKEN || '');
    setInputValue('DISCORD_CHANNEL_ID', config.DISCORD_CHANNEL_ID || '0');
    setInputValue('DISCORD_CONTENT_CHANNEL_ID', config.DISCORD_CONTENT_CHANNEL_ID || '0');
    setInputValue('DISCORD_ANNOUNCEMENTS_CHANNEL_ID', config.DISCORD_ANNOUNCEMENTS_CHANNEL_ID || '0');
    setInputValue('DISCORD_QUEUE_CHANNEL_ID', config.DISCORD_QUEUE_CHANNEL_ID || '0');
    setInputValue('DISCORD_STUDY_CHANNEL_ID', config.DISCORD_STUDY_CHANNEL_ID || '0');
    setInputValue('RENDER_URL', config.RENDER_URL || '');

    setInputValue('AI_PROVIDER', config.AI_PROVIDER || 'gemini');
    setInputValue('AI_FALLBACK_PROVIDER_1', config.AI_FALLBACK_PROVIDER_1 || 'gemini');
    setInputValue('AI_FALLBACK_PROVIDER_2', config.AI_FALLBACK_PROVIDER_2 || 'deepseek');
    setInputValue('AI_FALLBACK_PROVIDER_3', config.AI_FALLBACK_PROVIDER_3 || 'none');

    setInputValue('GEMINI_API_KEY', config.GEMINI_API_KEY || '');
    setInputValue('GEMINI_MODEL', config.GEMINI_MODEL || 'gemini-3.8-flash');
    setInputValue('GEMINI_FALLBACK_MODEL_1', config.GEMINI_FALLBACK_MODEL_1 || 'gemini-3.7-flash');
    setInputValue('GEMINI_FALLBACK_MODEL_2', config.GEMINI_FALLBACK_MODEL_2 || 'gemini-3.5-flash');
    setInputValue('GEMINI_FALLBACK_MODEL_3', config.GEMINI_FALLBACK_MODEL_3 || 'gemini-3.5-flash-lite');
    setInputValue('GEMINI_TIMEOUT_SECONDS', config.GEMINI_TIMEOUT_SECONDS || '90');
    setInputValue('GEMINI_FALLBACK_TIMEOUT_SECONDS', config.GEMINI_FALLBACK_TIMEOUT_SECONDS || '60');
    setInputValue('GEMINI_FALLBACK_DELAY_SECONDS', config.GEMINI_FALLBACK_DELAY_SECONDS || '2.0');

    setInputValue('ANTHROPIC_API_KEY', config.ANTHROPIC_API_KEY || '');
    setInputValue('ANTHROPIC_MODEL', config.ANTHROPIC_MODEL || 'claude-haiku-4-5');
    setInputValue('DEEPSEEK_API_KEY', config.DEEPSEEK_API_KEY || '');
    setInputValue('DEEPSEEK_MODEL', config.DEEPSEEK_MODEL || 'deepseek-flash');
    setInputValue('DEEPSEEK_BASE_URL', config.DEEPSEEK_BASE_URL || 'https://api.deepseek.com');
    setInputValue('DEEPSEEK_REASONING_EFFORT', config.DEEPSEEK_REASONING_EFFORT || 'high');
    setCheckboxValue('DEEPSEEK_THINKING_MODE', config.DEEPSEEK_THINKING_MODE !== 'false' && config.DEEPSEEK_THINKING_MODE !== false);

    const checkInterval = config.CHECK_INTERVAL_MINUTES || '30';
    setInputValue('CHECK_INTERVAL_MINUTES', checkInterval);
    setInputValue('CHECK_INTERVAL_MINUTES_SLIDER', checkInterval);
    setCheckboxValue('EMERGENCY_SUBMIT_ENABLED', config.EMERGENCY_SUBMIT_ENABLED === 'true');
    setCheckboxValue('AUTO_START_WINDOWS', config.AUTO_START_WINDOWS === 'true' || config.AUTO_START_WINDOWS === true);

    setInputValue('STORAGE_COOKIES_PATH', config.STORAGE_COOKIES_PATH || 'storage/cookies/session.json');
    setInputValue('STORAGE_MATERIALS_DIR', config.STORAGE_MATERIALS_DIR || 'storage/materials');
    setInputValue('STORAGE_SUBMISSIONS_DIR', config.STORAGE_SUBMISSIONS_DIR || 'storage/submissions');

    setInputValue('NOTION_API_KEY', config.NOTION_API_KEY || '');
    setInputValue('NOTION_PAGE_ID', config.NOTION_PAGE_ID || '17db4e452b43449a9ca266065840f909');
    setInputValue('NOTION_TASKS_DATABASE_ID', config.NOTION_TASKS_DATABASE_ID || '00e5c698-5139-4b4c-9cac-db04bfc22c4b');
    setInputValue('NOTION_COURSES_DATABASE_ID', config.NOTION_COURSES_DATABASE_ID || '751117de-c4d2-468c-9b46-571c036969b1');
    setInputValue('NOTION_DAILY_CHECKLIST_BLOCK_ID', config.NOTION_DAILY_CHECKLIST_BLOCK_ID || '25cd128a-26fe-49ac-8ab0-a895f1e0858d');
    setInputValue('NOTION_WEEKLY_SCHEDULE_TABLE_ID', config.NOTION_WEEKLY_SCHEDULE_TABLE_ID || '2a9222dd-474a-4c40-9b96-a548f2c9ec11');

  } catch (err) {
    showToast('Falha ao carregar configurações: ' + err.message, 'error');
  }
}

// Carrega Status do Sistema e Sessão (Moodle e Canvas LMS)
async function loadStatus() {
  try {
    const res = await fetch('/api/status');
    const status = await res.json();

    // -------------------------------------------------------------
    // 1. Status do Moodle (Sidebar)
    // -------------------------------------------------------------
    const moodleBadge = document.getElementById('moodle-session-badge');
    const moodleInfo = document.getElementById('moodle-session-info');
    const moodleDot = document.getElementById('moodle-status-dot');

    const isMoodleCreds = status.auth_mode === 'credentials';
    if (status.session_exists) {
      if (moodleBadge) {
        moodleBadge.textContent = 'Ativo';
        moodleBadge.className = 'badge badge-sm badge-success';
      }
      if (moodleDot) moodleDot.className = 'status-dot dot-active';
      if (moodleInfo) {
        moodleInfo.innerHTML = `Sessão ativa (${isMoodleCreds ? 'Auto' : 'Cookies'})<span class="subtext-date">${status.session_date || 'Recente'}</span>`;
      }
    } else if (isMoodleCreds && status.has_credentials) {
      if (moodleBadge) {
        moodleBadge.textContent = 'Pronto';
        moodleBadge.className = 'badge badge-sm badge-info';
      }
      if (moodleDot) moodleDot.className = 'status-dot dot-pending';
      if (moodleInfo) moodleInfo.textContent = 'Auto-login configurado';
    } else {
      if (moodleBadge) {
        moodleBadge.textContent = 'Pendente';
        moodleBadge.className = 'badge badge-sm badge-warning';
      }
      if (moodleDot) moodleDot.className = 'status-dot dot-off';
      if (moodleInfo) moodleInfo.textContent = isMoodleCreds ? 'Sem credenciais' : 'Requer login';
    }

    // Atualiza card interno da aba Moodle
    const boxTitle = document.getElementById('session-box-title');
    const boxDesc = document.getElementById('session-box-desc');
    if (boxTitle && boxDesc) {
      if (status.session_exists) {
        boxTitle.textContent = isMoodleCreds ? 'Sessão Ativa (Modo Automático)' : 'Sessão Ativa no Moodle';
        boxDesc.textContent = isMoodleCreds
          ? `Cookies válidos salvos (${status.session_date || 'Recente'}). Renovação automática habilitada.`
          : `Cookies válidos salvos (${status.session_date || 'Recente'}). Próximas varreduras autenticadas.`;
      } else {
        boxTitle.textContent = isMoodleCreds ? 'Login Automático Pendente' : 'Autenticação Pendente';
        boxDesc.textContent = isMoodleCreds
          ? 'O assistente fará login automaticamente usando suas credenciais salvas.'
          : 'O assistente precisa que você faça login no Moodle para salvar a sessão.';
      }
    }

    // -------------------------------------------------------------
    // 2. Status do Canvas LMS (Sidebar)
    // -------------------------------------------------------------
    const canvasBadge = document.getElementById('canvas-session-badge');
    const canvasInfo = document.getElementById('canvas-session-info');
    const canvasDot = document.getElementById('canvas-status-dot');

    const canvasMode = status.canvas_auth_mode || 'token';
    if (canvasMode === 'token') {
      if (status.canvas_has_token) {
        if (canvasBadge) {
          canvasBadge.textContent = 'Ativo';
          canvasBadge.className = 'badge badge-sm badge-success';
        }
        if (canvasDot) canvasDot.className = 'status-dot dot-active';
        if (canvasInfo) canvasInfo.textContent = 'Token REST v1 ativo';
      } else {
        if (canvasBadge) {
          canvasBadge.textContent = 'Pendente';
          canvasBadge.className = 'badge badge-sm badge-warning';
        }
        if (canvasDot) canvasDot.className = 'status-dot dot-off';
        if (canvasInfo) canvasInfo.textContent = 'Sem token informado';
      }
    } else if (canvasMode === 'cookies') {
      if (status.canvas_session_exists) {
        if (canvasBadge) {
          canvasBadge.textContent = 'Ativo';
          canvasBadge.className = 'badge badge-sm badge-success';
        }
        if (canvasDot) canvasDot.className = 'status-dot dot-active';
        if (canvasInfo) {
          canvasInfo.innerHTML = `Sessão ativa (Cookies)<span class="subtext-date">${status.canvas_session_date || 'Recente'}</span>`;
        }
      } else {
        if (canvasBadge) {
          canvasBadge.textContent = 'Pendente';
          canvasBadge.className = 'badge badge-sm badge-warning';
        }
        if (canvasDot) canvasDot.className = 'status-dot dot-off';
        if (canvasInfo) canvasInfo.textContent = 'Requer login navegador';
      }
    } else if (canvasMode === 'credentials') {
      if (status.canvas_session_exists) {
        if (canvasBadge) {
          canvasBadge.textContent = 'Ativo';
          canvasBadge.className = 'badge badge-sm badge-success';
        }
        if (canvasDot) canvasDot.className = 'status-dot dot-active';
        if (canvasInfo) {
          canvasInfo.innerHTML = `Sessão ativa (Auto)<span class="subtext-date">${status.canvas_session_date || 'Recente'}</span>`;
        }
      } else if (status.canvas_has_credentials) {
        if (canvasBadge) {
          canvasBadge.textContent = 'Pronto';
          canvasBadge.className = 'badge badge-sm badge-info';
        }
        if (canvasDot) canvasDot.className = 'status-dot dot-pending';
        if (canvasInfo) canvasInfo.textContent = 'Auto-login configurado';
      } else {
        if (canvasBadge) {
          canvasBadge.textContent = 'Pendente';
          canvasBadge.className = 'badge badge-sm badge-warning';
        }
        if (canvasDot) canvasDot.className = 'status-dot dot-off';
        if (canvasInfo) canvasInfo.textContent = 'Sem credenciais salvas';
      }
    }

    // Atualiza card interno da aba Canvas LMS
    const canvasBoxTitle = document.getElementById('canvas-session-box-title');
    const canvasBoxDesc = document.getElementById('canvas-session-box-desc');
    if (canvasBoxTitle && canvasBoxDesc) {
      if (status.canvas_session_exists) {
        canvasBoxTitle.textContent = 'Sessão do Canvas Ativa';
        canvasBoxDesc.textContent = `Cookies válidos salvos (${status.canvas_session_date || 'Recente'}). As chamadas à API REST utilizarão esta sessão.`;
      } else {
        canvasBoxTitle.textContent = 'Autenticação no Canvas Pendente';
        canvasBoxDesc.textContent = 'Nenhum cookie de sessão salvo. Clique abaixo para fazer login no Canvas pelo navegador.';
      }
    }
  } catch (err) {
    console.error('Erro ao ler status:', err);
  }
}

// Salva Configurações no .env
async function saveConfig() {
  const btn = document.getElementById('btn-save-env');
  const originalHtml = btn.innerHTML;
  btn.innerHTML = 'Salvando...';
  btn.disabled = true;

  const payload = {
    LMS_PROVIDER: document.querySelector('input[name="LMS_PROVIDER"]:checked')?.value || 'multi',
    CANVAS_BASE_URL: getInputValue('CANVAS_BASE_URL') || 'https://pucminas.instructure.com',
    CANVAS_AUTH_MODE: document.querySelector('input[name="CANVAS_AUTH_MODE"]:checked')?.value || 'token',
    CANVAS_API_TOKEN: getInputValue('CANVAS_API_TOKEN'),
    CANVAS_USERNAME: getInputValue('CANVAS_USERNAME'),
    CANVAS_PASSWORD: getInputValue('CANVAS_PASSWORD'),

    MOODLE_BASE_URL: getInputValue('MOODLE_BASE_URL'),
    AUTH_MODE: document.querySelector('input[name="AUTH_MODE"]:checked')?.value || 'cookies',
    MOODLE_USERNAME: getInputValue('MOODLE_USERNAME'),
    MOODLE_PASSWORD: getInputValue('MOODLE_PASSWORD'),
    HEADLESS_LOGIN: document.getElementById('HEADLESS_LOGIN').checked ? 'true' : 'false',
    LOGIN_TIMEOUT_SECONDS: getInputValue('LOGIN_TIMEOUT_SECONDS'),

    DISCORD_BOT_TOKEN: getInputValue('DISCORD_BOT_TOKEN'),
    DISCORD_CHANNEL_ID: getInputValue('DISCORD_CHANNEL_ID'),
    DISCORD_CONTENT_CHANNEL_ID: getInputValue('DISCORD_CONTENT_CHANNEL_ID'),
    DISCORD_ANNOUNCEMENTS_CHANNEL_ID: getInputValue('DISCORD_ANNOUNCEMENTS_CHANNEL_ID'),
    DISCORD_QUEUE_CHANNEL_ID: getInputValue('DISCORD_QUEUE_CHANNEL_ID'),
    DISCORD_STUDY_CHANNEL_ID: getInputValue('DISCORD_STUDY_CHANNEL_ID'),
    RENDER_URL: getInputValue('RENDER_URL'),

    AI_PROVIDER: getInputValue('AI_PROVIDER'),
    AI_FALLBACK_PROVIDER_1: getInputValue('AI_FALLBACK_PROVIDER_1'),
    AI_FALLBACK_PROVIDER_2: getInputValue('AI_FALLBACK_PROVIDER_2'),
    AI_FALLBACK_PROVIDER_3: getInputValue('AI_FALLBACK_PROVIDER_3'),

    GEMINI_API_KEY: getInputValue('GEMINI_API_KEY'),
    GEMINI_MODEL: getInputValue('GEMINI_MODEL'),
    GEMINI_FALLBACK_MODEL_1: getInputValue('GEMINI_FALLBACK_MODEL_1'),
    GEMINI_FALLBACK_MODEL_2: getInputValue('GEMINI_FALLBACK_MODEL_2'),
    GEMINI_FALLBACK_MODEL_3: getInputValue('GEMINI_FALLBACK_MODEL_3'),
    GEMINI_TIMEOUT_SECONDS: getInputValue('GEMINI_TIMEOUT_SECONDS'),
    GEMINI_FALLBACK_TIMEOUT_SECONDS: getInputValue('GEMINI_FALLBACK_TIMEOUT_SECONDS'),
    GEMINI_FALLBACK_DELAY_SECONDS: getInputValue('GEMINI_FALLBACK_DELAY_SECONDS'),

    ANTHROPIC_API_KEY: getInputValue('ANTHROPIC_API_KEY'),
    ANTHROPIC_MODEL: getInputValue('ANTHROPIC_MODEL'),
    DEEPSEEK_API_KEY: getInputValue('DEEPSEEK_API_KEY'),
    DEEPSEEK_MODEL: getInputValue('DEEPSEEK_MODEL'),
    DEEPSEEK_BASE_URL: getInputValue('DEEPSEEK_BASE_URL'),
    DEEPSEEK_REASONING_EFFORT: getInputValue('DEEPSEEK_REASONING_EFFORT'),
    DEEPSEEK_THINKING_MODE: document.getElementById('DEEPSEEK_THINKING_MODE')?.checked ? 'true' : 'false',

    CHECK_INTERVAL_MINUTES: getInputValue('CHECK_INTERVAL_MINUTES'),
    EMERGENCY_SUBMIT_ENABLED: document.getElementById('EMERGENCY_SUBMIT_ENABLED')?.checked ? 'true' : 'false',
    AUTO_START_WINDOWS: document.getElementById('AUTO_START_WINDOWS')?.checked ? 'true' : 'false',

    STORAGE_COOKIES_PATH: getInputValue('STORAGE_COOKIES_PATH'),

    STORAGE_MATERIALS_DIR: getInputValue('STORAGE_MATERIALS_DIR'),
    STORAGE_SUBMISSIONS_DIR: getInputValue('STORAGE_SUBMISSIONS_DIR'),

    NOTION_API_KEY: getInputValue('NOTION_API_KEY'),
    NOTION_PAGE_ID: getInputValue('NOTION_PAGE_ID'),
    NOTION_TASKS_DATABASE_ID: getInputValue('NOTION_TASKS_DATABASE_ID'),
    NOTION_COURSES_DATABASE_ID: getInputValue('NOTION_COURSES_DATABASE_ID'),
    NOTION_DAILY_CHECKLIST_BLOCK_ID: getInputValue('NOTION_DAILY_CHECKLIST_BLOCK_ID'),
    NOTION_WEEKLY_SCHEDULE_TABLE_ID: getInputValue('NOTION_WEEKLY_SCHEDULE_TABLE_ID'),
  };

  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const result = await res.json();
    if (result.ok) {
      showToast('Configurações salvas no .env com sucesso!', 'success');
    } else {
      showToast('Erro ao salvar: ' + (result.error || 'Desconhecido'), 'error');
    }
  } catch (err) {
    showToast('Erro na conexão com servidor: ' + err.message, 'error');
  } finally {
    btn.innerHTML = originalHtml;
    btn.disabled = false;
  }
}

// Troca programática de aba
function switchTab(tabId) {
  const item = document.querySelector(`.nav-item[data-tab="${tabId}"]`);
  if (item) {
    item.click();
  }
}

// Controle dinâmico das abas de LMS na barra lateral
function updateLmsTabsVisibility(provider) {
  const prov = (provider || 'multi').toLowerCase();
  const navCanvas = document.getElementById('nav-item-canvas');
  const navMoodle = document.getElementById('nav-item-moodle');
  const overviewCanvas = document.getElementById('overview-canvas-card');
  const overviewMoodle = document.getElementById('overview-moodle-card');
  const badgeCanvas = document.getElementById('overview-canvas-badge');
  const badgeMoodle = document.getElementById('overview-moodle-badge');

  if (prov === 'canvas') {
    if (navCanvas) navCanvas.style.display = 'flex';
    if (navMoodle) navMoodle.style.display = 'none';
    if (badgeCanvas) { badgeCanvas.textContent = 'Ativo'; badgeCanvas.className = 'badge badge-success'; }
    if (badgeMoodle) { badgeMoodle.textContent = 'Desativado'; badgeMoodle.className = 'badge badge-warning'; }
    if (overviewCanvas) overviewCanvas.style.opacity = '1';
    if (overviewMoodle) overviewMoodle.style.opacity = '0.4';
  } else if (prov === 'moodle') {
    if (navCanvas) navCanvas.style.display = 'none';
    if (navMoodle) navMoodle.style.display = 'flex';
    if (badgeCanvas) { badgeCanvas.textContent = 'Desativado'; badgeCanvas.className = 'badge badge-warning'; }
    if (badgeMoodle) { badgeMoodle.textContent = 'Ativo'; badgeMoodle.className = 'badge badge-success'; }
    if (overviewCanvas) overviewCanvas.style.opacity = '0.4';
    if (overviewMoodle) overviewMoodle.style.opacity = '1';
  } else {
    // multi (ambas)
    if (navCanvas) navCanvas.style.display = 'flex';
    if (navMoodle) navMoodle.style.display = 'flex';
    if (badgeCanvas) { badgeCanvas.textContent = 'Ativo'; badgeCanvas.className = 'badge badge-success'; }
    if (badgeMoodle) { badgeMoodle.textContent = 'Ativo'; badgeMoodle.className = 'badge badge-success'; }
    if (overviewCanvas) overviewCanvas.style.opacity = '1';
    if (overviewMoodle) overviewMoodle.style.opacity = '1';
  }

  // Se a aba atualmente aberta for desativada, redireciona suavemente para a central LMS
  const activePane = document.querySelector('.tab-pane.active')?.id;
  if ((prov === 'canvas' && activePane === 'tab-moodle') || (prov === 'moodle' && activePane === 'tab-canvas')) {
    switchTab('tab-lms');
  }
}

// Testes de Conexão
async function testCanvas() {
  const base_url = getInputValue('CANVAS_BASE_URL') || 'https://pucminas.instructure.com';
  const auth_mode = document.querySelector('input[name="CANVAS_AUTH_MODE"]:checked')?.value || 'token';
  const token = getInputValue('CANVAS_API_TOKEN');
  const username = getInputValue('CANVAS_USERNAME');
  const password = getInputValue('CANVAS_PASSWORD');
  const resultDiv = document.getElementById('canvas-test-result');
  setFeedback(resultDiv, 'Testando conectividade e autenticação com o Canvas LMS...', 'loading');

  try {
    const res = await fetch('/api/test-canvas', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ base_url, token, auth_mode, username, password })
    });
    const data = await res.json();
    if (data.ok) {
      setFeedback(resultDiv, `✔ ${data.message}`, 'success');
    } else {
      setFeedback(resultDiv, `✖ ${data.error}`, 'error');
    }
  } catch (err) {
    setFeedback(resultDiv, `✖ Erro ao testar: ${err.message}`, 'error');
  }
}

async function testMoodle() {
  const url = getInputValue('MOODLE_BASE_URL');
  const resultDiv = document.getElementById('moodle-test-result');
  setFeedback(resultDiv, 'Testando conectividade com o Moodle...', 'loading');

  try {
    const res = await fetch('/api/test-moodle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    const data = await res.json();
    if (data.ok) {
      setFeedback(resultDiv, `✔ ${data.message}`, 'success');
    } else {
      setFeedback(resultDiv, `✖ ${data.error}`, 'error');
    }
  } catch (err) {
    setFeedback(resultDiv, `✖ Erro ao testar: ${err.message}`, 'error');
  }
}

async function testDiscord() {
  const token = getInputValue('DISCORD_BOT_TOKEN');
  const channel_id = getInputValue('DISCORD_CHANNEL_ID');
  const resultDiv = document.getElementById('discord-test-result');
  setFeedback(resultDiv, 'Validando token junto ao Discord...', 'loading');

  try {
    const res = await fetch('/api/test-discord', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token, channel_id })
    });
    const data = await res.json();
    if (data.ok) {
      let msg = `✔ ${data.message}`;
      if (data.channel_info) {
        if (data.channel_info.ok) {
          msg += ` | Canal '${data.channel_info.name}' acessível.`;
        } else {
          msg += ` | Atenção: ${data.channel_info.error}`;
        }
      }
      setFeedback(resultDiv, msg, 'success');
    } else {
      setFeedback(resultDiv, `✖ ${data.error}`, 'error');
    }
  } catch (err) {
    setFeedback(resultDiv, `✖ Falha no teste: ${err.message}`, 'error');
  }
}

async function testGemini() {
  const api_key = getInputValue('GEMINI_API_KEY');
  const model = getInputValue('GEMINI_MODEL');
  const resultDiv = document.getElementById('gemini-test-result');
  setFeedback(resultDiv, `Enviando requisição de teste para o modelo ${model}...`, 'loading');

  try {
    const res = await fetch('/api/test-gemini', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key, model })
    });
    const data = await res.json();
    if (data.ok) {
      setFeedback(resultDiv, `✔ ${data.message} (Resposta: "${data.response}")`, 'success');
    } else {
      setFeedback(resultDiv, `✖ ${data.error}`, 'error');
    }
  } catch (err) {
    setFeedback(resultDiv, `✖ Erro no teste: ${err.message}`, 'error');
  }
}

async function testClaude() {
  const api_key = getInputValue('ANTHROPIC_API_KEY');
  const model = getInputValue('ANTHROPIC_MODEL');
  const resultDiv = document.getElementById('claude-test-result');
  setFeedback(resultDiv, `Enviando requisição de teste para o Claude (${model})...`, 'loading');

  try {
    const res = await fetch('/api/test-claude', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key, model })
    });
    const data = await res.json();
    if (data.ok) {
      setFeedback(resultDiv, `✔ ${data.message}`, 'success');
    } else {
      setFeedback(resultDiv, `✖ ${data.error}`, 'error');
    }
  } catch (err) {
    setFeedback(resultDiv, `✖ Erro no teste: ${err.message}`, 'error');
  }
}

async function testDeepSeek() {
  const api_key = getInputValue('DEEPSEEK_API_KEY');
  const model = getInputValue('DEEPSEEK_MODEL');
  const base_url = getInputValue('DEEPSEEK_BASE_URL');
  const reasoning_effort = getInputValue('DEEPSEEK_REASONING_EFFORT');
  const thinking_mode = document.getElementById('DEEPSEEK_THINKING_MODE')?.checked;
  const resultDiv = document.getElementById('deepseek-test-result');
  setFeedback(resultDiv, `Enviando requisição de teste para o DeepSeek (${model})...`, 'loading');

  try {
    const res = await fetch('/api/test-deepseek', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key, model, base_url, reasoning_effort, thinking_mode })
    });
    const data = await res.json();
    if (data.ok) {
      setFeedback(resultDiv, `✔ ${data.message}`, 'success');
    } else {
      setFeedback(resultDiv, `✖ ${data.error}`, 'error');
    }
  } catch (err) {
    setFeedback(resultDiv, `✖ Erro no teste: ${err.message}`, 'error');
  }
}

async function testNotion() {
  const api_key = getInputValue('NOTION_API_KEY');
  const page_id = getInputValue('NOTION_PAGE_ID');
  const tasks_db_id = getInputValue('NOTION_TASKS_DATABASE_ID');
  const courses_db_id = getInputValue('NOTION_COURSES_DATABASE_ID');
  const resultDiv = document.getElementById('notion-test-result');
  setFeedback(resultDiv, 'Verificando token de integração e databases no Notion...', 'loading');

  try {
    const res = await fetch('/api/test-notion', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key, page_id, tasks_db_id, courses_db_id })
    });
    const data = await res.json();
    if (data.ok) {
      setFeedback(resultDiv, `✔ ${data.message}`, 'success');
      showToast('Conexão com o Notion confirmada!', 'success');
    } else {
      setFeedback(resultDiv, `✖ ${data.error}`, 'error');
      showToast('Falha na validação do Notion', 'error');
    }
  } catch (err) {
    setFeedback(resultDiv, `✖ Erro no teste: ${err.message}`, 'error');
    showToast('Erro ao testar Notion: ' + err.message, 'error');
  }
}

async function triggerLogin() {
  const feedback = document.getElementById('login-feedback');
  setFeedback(feedback, 'Iniciando navegador Chromium para login no MinhaUFMG...', 'loading');

  try {
    const res = await fetch('/api/login-moodle', { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      setFeedback(feedback, '✔ Uma janela de navegador foi aberta! Digite suas credenciais e conclua o 2FA. Quando terminar, a janela fechará e o arquivo de sessão será salvo automaticamente.', 'success');
      showToast('Janela de login aberta no Windows!', 'info');
      // Monitora se o arquivo de cookies apareceu
      let checks = 0;
      const interval = setInterval(async () => {
        checks++;
        await loadStatus();
        if (checks >= 30) clearInterval(interval);
      }, 5000);
    } else {
      setFeedback(feedback, `✖ ${data.error}`, 'error');
    }
  } catch (err) {
    setFeedback(feedback, `✖ Erro ao disparar login: ${err.message}`, 'error');
  }
}

function updateAuthModeUI(mode) {
  const credBox = document.getElementById('credentials-box');
  const cookiesBox = document.getElementById('cookies-box');
  const cardCookies = document.getElementById('card-mode-cookies');
  const cardCreds = document.getElementById('card-mode-credentials');

  if (mode === 'credentials') {
    if (credBox) credBox.style.display = 'block';
    if (cardCreds) cardCreds.classList.add('active');
    if (cardCookies) cardCookies.classList.remove('active');
  } else {
    if (credBox) credBox.style.display = 'none';
    if (cardCookies) cardCookies.classList.add('active');
    if (cardCreds) cardCreds.classList.remove('active');
  }
}

function updateCanvasAuthModeUI(mode) {
  const tokenBox = document.getElementById('canvas-token-box');
  const credBox = document.getElementById('canvas-credentials-box');
  const cookiesBox = document.getElementById('canvas-cookies-box');
  const cardToken = document.getElementById('card-canvas-mode-token');
  const cardCookies = document.getElementById('card-canvas-mode-cookies');
  const cardCreds = document.getElementById('card-canvas-mode-credentials');

  [cardToken, cardCookies, cardCreds].forEach(c => c?.classList.remove('active'));

  if (mode === 'cookies') {
    if (tokenBox) tokenBox.style.display = 'none';
    if (credBox) credBox.style.display = 'none';
    if (cookiesBox) cookiesBox.style.display = 'block';
    if (cardCookies) cardCookies.classList.add('active');
  } else if (mode === 'credentials') {
    if (tokenBox) tokenBox.style.display = 'none';
    if (credBox) credBox.style.display = 'block';
    if (cookiesBox) cookiesBox.style.display = 'none';
    if (cardCreds) cardCreds.classList.add('active');
  } else {
    // token
    if (tokenBox) tokenBox.style.display = 'block';
    if (credBox) credBox.style.display = 'none';
    if (cookiesBox) cookiesBox.style.display = 'none';
    if (cardToken) cardToken.classList.add('active');
  }
}

async function triggerCanvasLogin() {
  const feedback = document.getElementById('canvas-login-feedback');
  setFeedback(feedback, 'Iniciando navegador Chromium para login no Canvas LMS...', 'loading');

  try {
    const res = await fetch('/api/login-canvas', { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      setFeedback(feedback, '✔ Uma janela de navegador foi aberta! Digite suas credenciais no Canvas. Ao concluir, a janela fechará e a sessão será salva automaticamente.', 'success');
      showToast('Janela de login no Canvas aberta!', 'info');
      let checks = 0;
      const interval = setInterval(async () => {
        checks++;
        await loadStatus();
        if (checks >= 30) clearInterval(interval);
      }, 5000);
    } else {
      setFeedback(feedback, `✖ ${data.error}`, 'error');
    }
  } catch (err) {
    setFeedback(feedback, `✖ Erro ao disparar login no Canvas: ${err.message}`, 'error');
  }
}

async function testCanvasCredentialsLogin() {
  const feedback = document.getElementById('canvas-cred-login-feedback');
  const base_url = getInputValue('CANVAS_BASE_URL') || 'https://pucminas.instructure.com';
  const username = getInputValue('CANVAS_USERNAME');
  const password = getInputValue('CANVAS_PASSWORD');

  if (!username || !password) {
    setFeedback(feedback, '✖ Preencha usuário e senha do Canvas antes de testar.', 'error');
    return;
  }

  setFeedback(feedback, 'Conectando ao Canvas e realizando login em segundo plano (headless)...', 'loading');

  try {
    const res = await fetch('/api/login-canvas-credentials', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ base_url, username, password, save: true })
    });
    const data = await res.json();
    if (data.ok) {
      setFeedback(feedback, `✔ ${data.message} A sessão foi salva e está pronta para uso!`, 'success');
      showToast('Login automático no Canvas concluído com sucesso!', 'success');
      await loadStatus();
    } else {
      setFeedback(feedback, `✖ ${data.error}`, 'error');
      showToast('Falha no login no Canvas: ' + data.error, 'error');
    }
  } catch (err) {
    setFeedback(feedback, `✖ Erro ao comunicar com o servidor: ${err.message}`, 'error');
  }
}

async function testCredentialsLogin() {
  const feedback = document.getElementById('cred-login-feedback');
  const username = getInputValue('MOODLE_USERNAME');
  const password = getInputValue('MOODLE_PASSWORD');

  if (!username || !password) {
    setFeedback(feedback, '✖ Preencha usuário e senha antes de testar.', 'error');
    return;
  }

  setFeedback(feedback, 'Conectando ao MinhaUFMG e realizando login em segundo plano (headless)...', 'loading');

  try {
    const res = await fetch('/api/login-credentials', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, save: true })
    });
    const data = await res.json();
    if (data.ok) {
      setFeedback(feedback, `✔ ${data.message} A sessão foi salva e está pronta para uso!`, 'success');
      showToast('Login automático concluído com sucesso!', 'success');
      await loadStatus();
    } else {
      setFeedback(feedback, `✖ ${data.error}`, 'error');
      showToast('Falha no login: ' + data.error, 'error');
    }
  } catch (err) {
    setFeedback(feedback, `✖ Erro ao comunicar com o servidor: ${err.message}`, 'error');
  }
}

async function openFolder(folderType) {
  try {
    const res = await fetch('/api/open-folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder: folderType })
    });
    const data = await res.json();
    if (data.ok) {
      showToast(data.message, 'info');
    } else {
      showToast(data.error, 'error');
    }
  } catch (err) {
    showToast('Erro ao abrir pasta: ' + err.message, 'error');
  }
}

// Helpers de Formulário
function getInputValue(id) {
  const el = document.getElementById(id);
  return el ? el.value.trim() : '';
}

function setInputValue(id, val) {
  const el = document.getElementById(id);
  if (el) el.value = val;
}

function setCheckboxValue(id, checked) {
  const el = document.getElementById(id);
  if (el) el.checked = Boolean(checked);
}

function setPreset(id, val) {
  setInputValue(id, val);
  showToast(`Preset aplicado: ${val}`, 'info');
}

function syncSlider(fromId, toId) {
  const fromEl = document.getElementById(fromId);
  const toEl = document.getElementById(toId);
  if (fromEl && toEl) toEl.value = fromEl.value;
}

function togglePassword(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.type = el.type === 'password' ? 'text' : 'password';
}

function setFeedback(el, msg, type) {
  if (!el) return;
  el.className = `test-feedback active ${type}`;
  el.textContent = msg;
}

// Toasts Notificações
function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.textContent = message;

  container.appendChild(toast);
  setTimeout(() => {
    if (toast.parentNode) toast.parentNode.removeChild(toast);
  }, 4500);
}
