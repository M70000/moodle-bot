// ===================================================================
// Moodle AI Assistant - Frontend Logic (Vanilla JS)
// ===================================================================

const tabMetadata = {
  'tab-moodle': {
    title: 'Moodle & Autenticação UFMG',
    subtitle: 'Configure a URL base do Moodle e a autenticação SSO via MinhaUFMG.'
  },
  'tab-discord': {
    title: 'Discord Bot & Notificações',
    subtitle: 'Configure o token do bot e os canais para recebimento de alertas e revisão.'
  },
  'tab-gemini': {
    title: 'Google Gemini IA',
    subtitle: 'Gerencie a chave de API e a hierarquia de modelos com fallback automático.'
  },
  'tab-scheduler': {
    title: 'Prazos & Agendamento',
    subtitle: 'Ajuste a frequência de varredura e regras de contagem regressiva.'
  },
  'tab-storage': {
    title: 'Armazenamento & Diretórios',
    subtitle: 'Pastas locais de cookies, materiais didáticos e rascunhos em PDF.'
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

  document.getElementById('btn-test-moodle').addEventListener('click', testMoodle);
  document.getElementById('btn-test-discord').addEventListener('click', testDiscord);
  document.getElementById('btn-test-gemini').addEventListener('click', testGemini);

  document.getElementById('btn-trigger-login').addEventListener('click', triggerLogin);
  document.getElementById('btn-login-quick').addEventListener('click', triggerLogin);
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

// Carrega Configuração do Backend
async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    const data = await res.json();
    const config = data.config || {};

    setInputValue('MOODLE_BASE_URL', config.MOODLE_BASE_URL || 'https://virtual.ufmg.br');
    setCheckboxValue('HEADLESS_LOGIN', config.HEADLESS_LOGIN === 'true');
    setInputValue('LOGIN_TIMEOUT_SECONDS', config.LOGIN_TIMEOUT_SECONDS || '300');

    setInputValue('DISCORD_BOT_TOKEN', config.DISCORD_BOT_TOKEN || '');
    setInputValue('DISCORD_CHANNEL_ID', config.DISCORD_CHANNEL_ID || '0');
    setInputValue('DISCORD_CONTENT_CHANNEL_ID', config.DISCORD_CONTENT_CHANNEL_ID || '0');
    setInputValue('DISCORD_ANNOUNCEMENTS_CHANNEL_ID', config.DISCORD_ANNOUNCEMENTS_CHANNEL_ID || '0');
    setInputValue('DISCORD_QUEUE_CHANNEL_ID', config.DISCORD_QUEUE_CHANNEL_ID || '0');

    setInputValue('GEMINI_API_KEY', config.GEMINI_API_KEY || '');
    setInputValue('GEMINI_MODEL', config.GEMINI_MODEL || 'gemini-3.8-flash');
    setInputValue('GEMINI_FALLBACK_MODEL_1', config.GEMINI_FALLBACK_MODEL_1 || 'gemini-3.7-flash');
    setInputValue('GEMINI_FALLBACK_MODEL_2', config.GEMINI_FALLBACK_MODEL_2 || 'gemini-3.5-flash');
    setInputValue('GEMINI_FALLBACK_MODEL_3', config.GEMINI_FALLBACK_MODEL_3 || 'gemini-3.5-flash-lite');
    setInputValue('GEMINI_TIMEOUT_SECONDS', config.GEMINI_TIMEOUT_SECONDS || '90');
    setInputValue('GEMINI_FALLBACK_TIMEOUT_SECONDS', config.GEMINI_FALLBACK_TIMEOUT_SECONDS || '60');
    setInputValue('GEMINI_FALLBACK_DELAY_SECONDS', config.GEMINI_FALLBACK_DELAY_SECONDS || '2.0');

    const checkInterval = config.CHECK_INTERVAL_MINUTES || '30';
    setInputValue('CHECK_INTERVAL_MINUTES', checkInterval);
    setInputValue('CHECK_INTERVAL_MINUTES_SLIDER', checkInterval);
    setCheckboxValue('EMERGENCY_SUBMIT_ENABLED', config.EMERGENCY_SUBMIT_ENABLED === 'true');

    setInputValue('STORAGE_COOKIES_PATH', config.STORAGE_COOKIES_PATH || 'storage/cookies/session.json');
    setInputValue('STORAGE_MATERIALS_DIR', config.STORAGE_MATERIALS_DIR || 'storage/materials');
    setInputValue('STORAGE_SUBMISSIONS_DIR', config.STORAGE_SUBMISSIONS_DIR || 'storage/submissions');
  } catch (err) {
    showToast('Falha ao carregar configurações: ' + err.message, 'error');
  }
}

// Carrega Status do Sistema e Sessão
async function loadStatus() {
  try {
    const res = await fetch('/api/status');
    const status = await res.json();

    const sessionBadge = document.getElementById('session-badge');
    const sessionInfo = document.getElementById('session-info');
    const boxTitle = document.getElementById('session-box-title');
    const boxDesc = document.getElementById('session-box-desc');

    if (status.session_exists) {
      sessionBadge.textContent = 'Autenticado';
      sessionBadge.className = 'badge badge-success';
      sessionInfo.innerHTML = `Sessão ativa<br><small class="text-muted">${status.session_date || 'Recente'}</small>`;
      boxTitle.textContent = 'Sessão Ativa no MinhaUFMG';
      boxDesc.textContent = `Cookies válidos salvos (${status.session_date}). As próximas varreduras rodarão em segundo plano sem pedir senha.`;
    } else {
      sessionBadge.textContent = 'Pendente';
      sessionBadge.className = 'badge badge-warning';
      sessionInfo.textContent = 'Nenhuma sessão encontrada. Clique abaixo para logar.';
      boxTitle.textContent = 'Autenticação Pendente';
      boxDesc.textContent = 'O assistente precisa que você faça login no MinhaUFMG uma vez para salvar a sessão.';
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
    MOODLE_BASE_URL: getInputValue('MOODLE_BASE_URL'),
    HEADLESS_LOGIN: document.getElementById('HEADLESS_LOGIN').checked ? 'true' : 'false',
    LOGIN_TIMEOUT_SECONDS: getInputValue('LOGIN_TIMEOUT_SECONDS'),

    DISCORD_BOT_TOKEN: getInputValue('DISCORD_BOT_TOKEN'),
    DISCORD_CHANNEL_ID: getInputValue('DISCORD_CHANNEL_ID'),
    DISCORD_CONTENT_CHANNEL_ID: getInputValue('DISCORD_CONTENT_CHANNEL_ID'),
    DISCORD_ANNOUNCEMENTS_CHANNEL_ID: getInputValue('DISCORD_ANNOUNCEMENTS_CHANNEL_ID'),
    DISCORD_QUEUE_CHANNEL_ID: getInputValue('DISCORD_QUEUE_CHANNEL_ID'),

    GEMINI_API_KEY: getInputValue('GEMINI_API_KEY'),
    GEMINI_MODEL: getInputValue('GEMINI_MODEL'),
    GEMINI_FALLBACK_MODEL_1: getInputValue('GEMINI_FALLBACK_MODEL_1'),
    GEMINI_FALLBACK_MODEL_2: getInputValue('GEMINI_FALLBACK_MODEL_2'),
    GEMINI_FALLBACK_MODEL_3: getInputValue('GEMINI_FALLBACK_MODEL_3'),
    GEMINI_TIMEOUT_SECONDS: getInputValue('GEMINI_TIMEOUT_SECONDS'),
    GEMINI_FALLBACK_TIMEOUT_SECONDS: getInputValue('GEMINI_FALLBACK_TIMEOUT_SECONDS'),
    GEMINI_FALLBACK_DELAY_SECONDS: getInputValue('GEMINI_FALLBACK_DELAY_SECONDS'),

    CHECK_INTERVAL_MINUTES: getInputValue('CHECK_INTERVAL_MINUTES'),
    EMERGENCY_SUBMIT_ENABLED: document.getElementById('EMERGENCY_SUBMIT_ENABLED').checked ? 'true' : 'false',

    STORAGE_COOKIES_PATH: getInputValue('STORAGE_COOKIES_PATH'),
    STORAGE_MATERIALS_DIR: getInputValue('STORAGE_MATERIALS_DIR'),
    STORAGE_SUBMISSIONS_DIR: getInputValue('STORAGE_SUBMISSIONS_DIR'),
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

// Testes de Conexão
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
