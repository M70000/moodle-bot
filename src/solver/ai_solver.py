"""Camada de abstração multi-provider para resolução de atividades com IA.

Suporta três provedores BYOK configuráveis:
  - Google Gemini    (GEMINI_API_KEY)    → google-genai SDK
  - Anthropic Claude (ANTHROPIC_API_KEY) → anthropic SDK
  - DeepSeek         (DEEPSEEK_API_KEY)  → openai SDK com base_url customizada

O usuário pode escolher explicitamente o Provedor Principal (AI_PROVIDER)
e configurar uma Cadeia de Contingência / Fallback dinâmica entre os três provedores:
(AI_FALLBACK_PROVIDER_1, AI_FALLBACK_PROVIDER_2, AI_FALLBACK_PROVIDER_3).

Se o provedor principal sofrer timeout, estourar cotas (429) ou ficar indisponível,
o robô migra instantaneamente para o próximo provedor configurado na cadeia.
"""

import asyncio
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from rich.console import Console

from config.settings import settings
from src.scraper.moodle_scraper import Assignment

console = Console()


def normalize_provider(name: Optional[str]) -> Optional[str]:
    """Normaliza o nome do provedor para um identificador canônico ou None."""
    if not name:
        return None
    cleaned = str(name).strip().lower()
    if cleaned in ("gemini", "google", "google-gemini", "google_gemini"):
        return "gemini"
    if cleaned in ("claude", "anthropic", "anthropic-claude", "anthropic_claude"):
        return "anthropic"
    if cleaned in ("deepseek", "deep-seek", "deep_seek"):
        return "deepseek"
    if cleaned in ("none", "nenhum", "desativado", "disabled", "off", "0", ""):
        return None
    return cleaned


def get_fallback_chain() -> List[str]:
    """Retorna a lista ordenada dos provedores configurados para execução e contingência."""
    primary = normalize_provider(getattr(settings, "AI_PROVIDER", "gemini")) or "gemini"

    raw_candidates = [
        primary,
        normalize_provider(getattr(settings, "AI_FALLBACK_PROVIDER_1", None)),
        normalize_provider(getattr(settings, "AI_FALLBACK_PROVIDER_2", None)),
        normalize_provider(getattr(settings, "AI_FALLBACK_PROVIDER_3", None)),
    ]

    chain: List[str] = []
    for c in raw_candidates:
        if c and c not in chain:
            chain.append(c)

    if not chain:
        chain = ["gemini"]

    return chain


def get_active_provider() -> str:
    """Retorna descrição clara do provedor principal e sua cadeia de contingência para o /status."""
    chain = get_fallback_chain()
    primary = chain[0] if chain else "gemini"

    def _format_prov(p: str) -> str:
        if p == "anthropic":
            model = settings.ANTHROPIC_MODEL or "claude-haiku-4-5"
            return f"Anthropic Claude ({model})"
        elif p == "deepseek":
            model = settings.DEEPSEEK_MODEL or "deepseek-chat"
            return f"DeepSeek ({model})"
        else:
            model = settings.GEMINI_MODEL or "gemini-3.5-flash"
            return f"Google Gemini ({model})"

    primary_str = _format_prov(primary)
    fallbacks = chain[1:]
    if fallbacks:
        fb_names = []
        for fb in fallbacks:
            if fb == "anthropic":
                fb_names.append("Claude")
            elif fb == "deepseek":
                fb_names.append("DeepSeek")
            else:
                fb_names.append("Gemini")
        return f"{primary_str} [Fallback: {' → '.join(fb_names)}]"
    return primary_str


async def _emit_log(callback: Optional[Any], msg: str):
    """Envia mensagem para o callback de log ao vivo de forma segura."""
    if not callback:
        return
    try:
        res = callback(msg)
        if asyncio.iscoroutine(res) or isinstance(res, asyncio.Future):
            await res
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Backends de Execução (Claude e DeepSeek)
# ---------------------------------------------------------------------------

async def _call_claude(
    system_instruction: str,
    user_message: str,
    on_log: Optional[Any] = None,
) -> Tuple[str, str]:
    """Chama a API do Anthropic Claude e retorna (texto, modelo_usado)."""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError(
            "SDK do Anthropic não instalado. Execute: pip install anthropic"
        )

    api_key = settings.ANTHROPIC_API_KEY
    if not api_key or api_key in ("", "sua_chave_anthropic_aqui"):
        raise RuntimeError("Chave ANTHROPIC_API_KEY não configurada no .env.")

    model = settings.ANTHROPIC_MODEL or "claude-haiku-4-5"
    await _emit_log(on_log, f"Consultando Anthropic Claude ({model})...")

    client = anthropic.AsyncAnthropic(api_key=api_key)
    response = await asyncio.wait_for(
        client.messages.create(
            model=model,
            max_tokens=4096,
            system=system_instruction,
            messages=[{"role": "user", "content": user_message}],
        ),
        timeout=120,
    )
    text = response.content[0].text if response.content else ""
    await _emit_log(on_log, f"✔ Resposta do Claude ({model}) recebida.")
    return text, model


async def _call_deepseek(
    system_instruction: str,
    user_message: str,
    on_log: Optional[Any] = None,
    json_output: bool = False,
    images: Optional[List[Union[Path, str, bytes]]] = None,
    thinking_mode: Optional[bool] = None,
    reasoning_effort: Optional[str] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_call_handler: Optional[Callable] = None,
) -> Tuple[str, str]:
    """Chama a API do DeepSeek com suporte a deepseek-flash, Visão multimodal, JSON Output e Thinking CoT."""
    try:
        from openai import AsyncOpenAI
    except ImportError:
        raise RuntimeError(
            "SDK da OpenAI não instalado (necessário para DeepSeek). Execute: pip install openai"
        )

    api_key = settings.DEEPSEEK_API_KEY
    if not api_key or api_key in ("", "sua_chave_deepseek_aqui"):
        raise RuntimeError("Chave DEEPSEEK_API_KEY não configurada no .env.")

    model = settings.DEEPSEEK_MODEL or "deepseek-flash"
    base_url = getattr(settings, "DEEPSEEK_BASE_URL", "https://api.deepseek.com") or "https://api.deepseek.com"
    await _emit_log(on_log, f"Consultando DeepSeek ({model})...")

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
    )

    # 1. Preparação do prompt de sistema (garantindo palavra 'json' se json_output=True)
    sys_content = system_instruction
    if json_output and "json" not in sys_content.lower() and "json" not in user_message.lower():
        sys_content += "\nResponda estritamente em formato JSON válido."

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": sys_content}
    ]

    # 2. Suporte à Visão Multimodal (deepseek-flash aceita imagens em base64 inline no conteúdo de usuário)
    user_content_parts: List[Dict[str, Any]] = [
        {"type": "text", "text": user_message}
    ]

    if images:
        import base64
        for img in images:
            b64_data = ""
            mime_type = "image/jpeg"
            if isinstance(img, (str, Path)):
                p = Path(img)
                if p.exists() and p.is_file():
                    suffix = p.suffix.lower()
                    if suffix in (".png", ".webp", ".gif"):
                        mime_type = f"image/{suffix[1:]}"
                    elif suffix in (".jpg", ".jpeg"):
                        mime_type = "image/jpeg"
                    b64_data = base64.b64encode(p.read_bytes()).decode("utf-8")
            elif isinstance(img, bytes):
                b64_data = base64.b64encode(img).decode("utf-8")

            if b64_data:
                user_content_parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{b64_data}"
                    }
                })

    if len(user_content_parts) > 1:
        messages.append({"role": "user", "content": user_content_parts})
    else:
        messages.append({"role": "user", "content": user_message})

    # 3. Configuração de parâmetros de inferência
    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": 4096,
    }

    # JSON Output Estrito
    if json_output:
        kwargs["response_format"] = {"type": "json_object"}

    # Thinking Mode (Chain-of-Thought) no deepseek-flash ou deepseek-reasoner
    is_thinking = thinking_mode if thinking_mode is not None else getattr(settings, "DEEPSEEK_THINKING_MODE", True)
    if "flash" in model.lower() or "reasoner" in model.lower():
        if is_thinking:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
            kwargs["reasoning_effort"] = reasoning_effort or getattr(settings, "DEEPSEEK_REASONING_EFFORT", "high") or "high"
        else:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    else:
        kwargs["temperature"] = 0.1

    if tools:
        kwargs["tools"] = tools

    # 4. Execução (com suporte a Tool Calls iterativos se handler fornecido)
    sub_turn = 1
    while True:
        response = await asyncio.wait_for(
            client.chat.completions.create(**kwargs),
            timeout=120,
        )
        choice = response.choices[0]
        msg = choice.message
        tool_calls = getattr(msg, "tool_calls", None)

        if not tool_calls or not tool_call_handler:
            break

        # Injeta reasoning_content e mensagem do assistente antes dos resultados das ferramentas
        messages.append(msg)
        import json as _j
        for tc in tool_calls:
            fn_name = tc.function.name
            fn_args = _j.loads(tc.function.arguments) if tc.function.arguments else {}
            tool_result = await tool_call_handler(fn_name, fn_args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": str(tool_result),
            })

        kwargs["messages"] = messages
        sub_turn += 1
        if sub_turn > 10:
            break

    # 5. Captura de Raciocínio (Chain-of-Thought) e Conteúdo Final
    choice = response.choices[0]
    msg = choice.message
    reasoning_content = getattr(msg, "reasoning_content", None)
    if reasoning_content:
        preview = reasoning_content.strip().replace("\n", " ")
        if len(preview) > 140:
            preview = preview[:140] + "..."
        await _emit_log(on_log, f"🧠 Raciocínio CoT (DeepSeek Flash): {preview}")

    text = msg.content or ""
    await _emit_log(on_log, f"✔ Resposta do DeepSeek ({model}) recebida.")
    return text, model


# ---------------------------------------------------------------------------
# AISolver — Fachada Unificada com Cadeia de Fallback Dinâmica
# ---------------------------------------------------------------------------

class AISolver:
    """Motor de resolução unificado com suporte flexível a Gemini, Claude e DeepSeek.

    Permite escolher o provedor principal e encadear múltiplos provedores
    de contingência em caso de erro, rate limit ou timeout.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.chain = get_fallback_chain()
        self.provider = self.chain[0] if self.chain else "gemini"
        self._gemini_solver = None

        # Sempre prepara GeminiSolver se estiver na cadeia ou como rede de segurança
        if "gemini" in self.chain or not self.chain:
            from src.solver.gemini_solver import GeminiSolver
            self._gemini_solver = GeminiSolver(api_key=api_key)

        console.print(
            f"[cyan]🤖 Provedor IA ativo: [bold]{get_active_provider()}[/bold][/cyan]"
        )

    def _is_provider_ready(self, provider: str) -> Tuple[bool, str]:
        """Verifica se as credenciais do provedor estão presentes."""
        if provider == "gemini":
            key = settings.GEMINI_API_KEY
            if not key or key in ("", "sua_chave_gemini_api_aqui"):
                return False, "GEMINI_API_KEY ausente ou não configurada no .env"
            return True, ""
        elif provider == "anthropic":
            key = settings.ANTHROPIC_API_KEY
            if not key or key in ("", "sua_chave_anthropic_aqui"):
                return False, "ANTHROPIC_API_KEY ausente ou não configurada no .env"
            return True, ""
        elif provider == "deepseek":
            key = settings.DEEPSEEK_API_KEY
            if not key or key in ("", "sua_chave_deepseek_aqui"):
                return False, "DEEPSEEK_API_KEY ausente ou não configurada no .env"
            return True, ""
        return False, f"Provedor desconhecido: {provider}"

    async def generate_text(
        self,
        system_instruction: str,
        user_message: str,
        temperature: float = 0.2,
        on_log: Optional[Any] = None,
        json_output: bool = False,
        images: Optional[List[Any]] = None,
        thinking_mode: Optional[bool] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_call_handler: Optional[Callable] = None,
    ) -> Tuple[str, str]:
        """Gera texto puro percorrendo a cadeia de provedores configurada."""
        last_error = None

        for idx, prov in enumerate(self.chain):
            is_ready, reason = self._is_provider_ready(prov)
            if not is_ready:
                console.print(f"[dim]Pulando {prov}: {reason}[/dim]")
                continue

            try:
                if prov == "gemini":
                    if not self._gemini_solver:
                        from src.solver.gemini_solver import GeminiSolver
                        self._gemini_solver = GeminiSolver()
                    resp, used_model = await self._gemini_solver._generate_with_fallback(
                        contents=[user_message],
                        system_instruction=system_instruction,
                        temperature=temperature,
                        on_log=on_log,
                    )
                    text = resp.text if resp and resp.text else ""
                    return text, used_model

                elif prov == "anthropic":
                    return await _call_claude(system_instruction, user_message, on_log=on_log)

                elif prov == "deepseek":
                    return await _call_deepseek(
                        system_instruction=system_instruction,
                        user_message=user_message,
                        on_log=on_log,
                        json_output=json_output,
                        images=images,
                        thinking_mode=thinking_mode,
                        tools=tools,
                        tool_call_handler=tool_call_handler,
                    )

            except Exception as err:
                last_error = err
                console.print(f"[yellow]Aviso: Falha no provedor {prov}: {err}[/yellow]")
                if idx < len(self.chain) - 1:
                    next_prov = self.chain[idx + 1]
                    await _emit_log(on_log, f"⚠️ Falha em {prov}. Acionando fallback: {next_prov}...")

        raise RuntimeError(
            f"Todos os provedores de IA da cadeia falharam ({' → '.join(self.chain)}). "
            f"Último erro: {last_error}"
        )

    async def generate_json(
        self,
        system_instruction: str,
        user_message: str,
        on_log: Optional[Any] = None,
        images: Optional[List[Any]] = None,
    ) -> Tuple[Union[Dict[str, Any], List[Any]], str]:
        """Gera resposta em formato JSON estrito, já convertida em dicionário ou lista Python."""
        raw_text, used_model = await self.generate_text(
            system_instruction=system_instruction,
            user_message=user_message,
            on_log=on_log,
            json_output=True,
            images=images,
        )
        import json as _j
        import re as _r
        cleaned = raw_text.strip()
        match = _r.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
        if match:
            cleaned = match.group(1).strip()
        return _j.loads(cleaned), used_model

    async def solve_assignment(
        self,
        assignment: Assignment,
        user_notes: Optional[str] = None,
        extra_context_files: Optional[List[Path]] = None,
        on_log: Optional[Any] = None,
        auto_triggered: bool = False,
    ):
        """Gera resolução completa delegando através da cadeia de contingência configurada."""
        from src.solver.gemini_solver import extract_context_materials

        context_files = []
        for att in getattr(assignment, "attachments", []):
            if att.local_path and att.local_path.exists():
                context_files.append(att.local_path)
        if extra_context_files:
            context_files.extend([f for f in extra_context_files if f.exists()])

        extracted_ref, extracted_images, used_material_names = extract_context_materials(context_files)

        is_coding = (
            getattr(assignment, "is_coding_task", False)
            or any(k in (assignment.title or "").lower() for k in ["coding task", "programação", "código", "python"])
            or "snapshot to url" in (assignment.description or "").lower()
            or "snapshot" in (assignment.description or "").lower()
        )

        if is_coding:
            system_instruction = (
                "Você é um desenvolvedor de software e estudante resolvendo uma tarefa prática de programação (Coding Task).\n"
                "Seu foco principal é fornecer EXCLUSIVAMENTE o código correto, limpo e executável para rodar no editor interativo e gerar a saída esperada.\n\n"
                "DIRETRIZES OBRIGATÓRIAS:\n"
                "1. DIRETO AO PONTO: É ESTRITAMENTE PROIBIDO escrever relatórios teóricos longos, introduções acadêmicas, tutoriais ou explicações desnecessárias sobre a sintaxe.\n"
                "2. CÓDIGO EXECUTÁVEL: Forneça a solução completa, funcional e limpa dentro de um bloco de código markdown:\n"
                "```python\n"
                "[código funcional aqui]\n"
                "```\n"
                "3. No máximo 1 ou 2 linhas objetivas indicando o que o código faz, sem rodeios.\n"
                "4. NUNCA invente blocos de texto prolixos de IA."
            )
        else:
            system_instruction = (
                "Você é um estudante universitário da UFMG realizando esta atividade acadêmica. "
                "Resolva de forma clara, direta e acadêmica, sem texto introdutório de IA ou metatexto.\n\n"
                "DIRETRIZES DE RESOLUÇÃO:\n"
                "1. REGRA DE OURO - Se houver materiais de apoio, gabarito ou anotações fornecidos contendo respostas para esta atividade, siga 100% as respostas e termos indicados neles.\n"
                "2. FOCO TOTAL NA ATIVIDADE ESPECÍFICA: Resolva exclusivamente o que foi pedido no enunciado.\n"
                "3. FORMATO OBRIGATÓRIO (DUAS PARTES):\n"
                "   PARTE 1: Folha de Respostas Acadêmica (para leitura e conferência):\n"
                "     ### Questão 1\n"
                "     - **Resposta:** [Sua resposta direta e fundamentada]\n\n"
                "     ### Questão 2\n"
                "     - **Resposta:** [Sua resposta]\n\n"
                "   PARTE 2: Dados Estruturados (no final, se aplicável):\n"
                "   ```json:answers\n"
                "   [\n"
                '     {"key": "Q1", "value": "resposta 1"},\n'
                '     {"key": "Q2", "value": "resposta 2"}\n'
                "   ]\n"
                "   ```"
            )

        user_message = (
            f"DISCIPLINA: {assignment.course_name}\n"
            f"ATIVIDADE: {assignment.title}\n"
            f"ENUNCIADO:\n{assignment.description}\n"
        )
        if extracted_ref:
            user_message += f"\nMATERIAL DE APOIO:\n{extracted_ref}\n"
        if user_notes:
            user_message += f"\nINSTRUÇÕES DO ALUNO:\n{user_notes}\n"

        last_error = None

        for idx, prov in enumerate(self.chain):
            is_ready, reason = self._is_provider_ready(prov)
            if not is_ready:
                console.print(f"[dim]Pulando {prov} na resolução: {reason}[/dim]")
                continue

            try:
                if prov == "gemini" and self._gemini_solver and self._gemini_solver.client:
                    # Executa via GeminiSolver nativo (com suporte à API de arquivos e seus próprios fallbacks)
                    return await self._gemini_solver.solve_assignment(
                        assignment=assignment,
                        user_notes=user_notes,
                        extra_context_files=extra_context_files,
                        on_log=on_log,
                        auto_triggered=auto_triggered,
                    )
                elif prov == "anthropic":
                    full_text, used_model = await _call_claude(system_instruction, user_message, on_log)
                    return await _build_solution_draft(
                        assignment=assignment,
                        full_text=full_text,
                        used_model=used_model,
                        on_log=on_log,
                        auto_triggered=auto_triggered,
                        used_materials=used_material_names,
                    )
                elif prov == "deepseek":
                    full_text, used_model = await _call_deepseek(
                        system_instruction,
                        user_message,
                        on_log,
                        images=extracted_images if extracted_images else None,
                    )
                    return await _build_solution_draft(
                        assignment=assignment,
                        full_text=full_text,
                        used_model=used_model,
                        on_log=on_log,
                        auto_triggered=auto_triggered,
                        used_materials=used_material_names,
                    )
                elif prov == "gemini":
                    # Gemini sem cliente completo ou fallback geral
                    full_text, used_model = await self.generate_text(system_instruction, user_message, on_log=on_log)
                    return await _build_solution_draft(
                        assignment=assignment,
                        full_text=full_text,
                        used_model=used_model,
                        on_log=on_log,
                        auto_triggered=auto_triggered,
                        used_materials=used_material_names,
                    )

            except Exception as err:
                last_error = err
                console.print(f"[yellow]Aviso: Falha ao resolver com {prov}: {err}[/yellow]")
                if idx < len(self.chain) - 1:
                    next_prov = self.chain[idx + 1]
                    await _emit_log(on_log, f"⚠️ Falha no provedor {prov}. Alternando para contingência: {next_prov}...")

        raise RuntimeError(
            f"Todos os provedores configurados falharam para esta atividade ({' → '.join(self.chain)}). "
            f"Último erro: {last_error}"
        )

    async def solve_quiz_with_live_context(
        self,
        assignment: Assignment,
        questions_data: List[Dict[str, Any]],
        user_notes: Optional[str] = None,
        extra_context_files: Optional[List[Path]] = None,
        on_log: Optional[Any] = None,
        auto_triggered: bool = False,
    ):
        """Resolve questionário ao vivo delegando através da cadeia de contingência."""
        from src.solver.gemini_solver import extract_context_materials

        context_files = []
        for att in getattr(assignment, "attachments", []):
            if att.local_path and att.local_path.exists():
                context_files.append(att.local_path)
        if extra_context_files:
            context_files.extend([f for f in extra_context_files if f.exists()])

        extracted_ref, extracted_images, used_material_names = extract_context_materials(context_files)

        formatted_questions = []
        for q in questions_data:
            q_txt = q.get("fullTextWithTokens", "").strip()
            if q_txt:
                formatted_questions.append(f"### {q.get('qNumberText', 'Questão')}\n{q_txt}")
        questions_body = "\n\n".join(formatted_questions)

        system_instruction = (
            "Você é um estudante universitário da UFMG realizando uma atividade avaliativa no Moodle.\n"
            "Abaixo está o conteúdo extraído da tela do questionário, contendo questões avaliativas que podem conter:\n"
            "- Marcadores pontuais [[CAMPO_1]], [[CAMPO_2]]... que representam lacunas, caixas de texto, áreas de arrastar/soltar ou questões dissertativas;\n"
            "- Questões de múltipla escolha com alternativas (ex: a, b, c, d);\n"
            "- Questões de seleção múltipla (caixas de seleção / checkboxes);\n"
            "- Questões abertas/dissertativas que exigem redação de resposta fundamentada.\n\n"
            "DIRETRIZES DE RESOLUÇÃO:\n"
            "1. REGRA DE OURO - PRIORIDADE ABSOLUTA DO GABARITO / MATERIAL DE APOIO FORNECIDO:\n"
            "   - Se houver materiais de apoio, gabarito ou anotações fornecidos, siga 100% as respostas e termos indicados neles.\n"
            "   - É PROIBIDO divergir ou tentar 're-resolver' uma questão que já possui resposta no material.\n"
            "2. PREENCHA CADA CAMPO E QUESTÃO: Forneça a resposta para cada marcador [[CAMPO_X]], questão de múltipla escolha (Q1, Q2, etc.) e questão dissertativa.\n"
            "3. ALTERNATIVAS DE MÚLTIPLA ESCOLHA: Indique a letra E o texto completo da alternativa escolhida (ex: 'a. fast').\n"
            "4. CORRESPONDÊNCIA / LACUNAS: Numere-os na folha de respostas (1. **item 1**, 2. **item 2**...).\n\n"
            "FORMATO OBRIGATÓRIO DE SAÍDA (DUAS PARTES ESTRITAMENTE OBRIGATÓRIAS):\n\n"
            "PARTE 1: Bloco JSON estruturado no início (usado pelo robô para preenchimento automático no Moodle):\n"
            "```json:answers\n"
            "{\n"
            '  "CAMPO_1": "resposta da lacuna ou palavra arrastada",\n'
            '  "Q1": "letra e texto completo da alternativa escolhida (ex: a. fast)",\n'
            '  "Q2": "texto da resposta dissertativa ou alternativas"\n'
            "}\n"
            "```\n\n"
            "PARTE 2: Folha de Respostas Acadêmica (renderizada no documento DOCX/PDF para leitura humana):\n"
            "Logo abaixo do bloco JSON, escreva a folha de respostas limpa e organizada:\n"
            "- Separe estritamente por questão avaliativa (ex: '### Questão 1', '### Questão 2').\n"
            "- Para cada questão, escreva:\n"
            "  - **Resposta:** [letra e texto completo da alternativa, ou texto dissertativo]\n"
            "- NUNCA omita a PARTE 2. Ambas as partes são estritamente obrigatórias."
        )

        user_message = (
            f"DISCIPLINA: {assignment.course_name}\n"
            f"ATIVIDADE: {assignment.title}\n\n"
            f"QUESTIONÁRIO:\n{questions_body}\n"
        )
        if extracted_ref:
            user_message += f"\nMATERIAL DE APOIO:\n{extracted_ref}\n"
        if user_notes:
            user_message += f"\nINSTRUÇÕES DO ALUNO:\n{user_notes}\n"

        last_error = None

        for idx, prov in enumerate(self.chain):
            is_ready, reason = self._is_provider_ready(prov)
            if not is_ready:
                console.print(f"[dim]Pulando {prov} no questionário: {reason}[/dim]")
                continue

            try:
                if prov == "gemini" and self._gemini_solver and self._gemini_solver.client:
                    return await self._gemini_solver.solve_quiz_with_live_context(
                        assignment=assignment,
                        questions_data=questions_data,
                        user_notes=user_notes,
                        extra_context_files=extra_context_files,
                        on_log=on_log,
                        auto_triggered=auto_triggered,
                    )
                elif prov == "anthropic":
                    full_text, used_model = await _call_claude(system_instruction, user_message, on_log)
                    return await _build_solution_draft(
                        assignment=assignment,
                        full_text=full_text,
                        used_model=used_model,
                        on_log=on_log,
                        auto_triggered=auto_triggered,
                        is_quiz=True,
                        used_materials=used_material_names,
                    )
                elif prov == "deepseek":
                    full_text, used_model = await _call_deepseek(
                        system_instruction,
                        user_message,
                        on_log,
                        images=extracted_images if extracted_images else None,
                    )
                    return await _build_solution_draft(
                        assignment=assignment,
                        full_text=full_text,
                        used_model=used_model,
                        on_log=on_log,
                        auto_triggered=auto_triggered,
                        is_quiz=True,
                        used_materials=used_material_names,
                    )

            except Exception as err:
                last_error = err
                console.print(f"[yellow]Aviso: Falha no quiz com {prov}: {err}[/yellow]")
                if idx < len(self.chain) - 1:
                    next_prov = self.chain[idx + 1]
                    await _emit_log(on_log, f"⚠️ Falha no quiz via {prov}. Alternando para contingência: {next_prov}...")

        raise RuntimeError(
            f"Todos os provedores falharam para este questionário ({' → '.join(self.chain)}). "
            f"Último erro: {last_error}"
        )

    async def apply_revision(
        self,
        draft,
        revision_instructions: str,
        on_log: Optional[Any] = None,
    ):
        """Aplica revisão do usuário sobre um draft existente percorrendo a cadeia de contingência."""
        is_quiz = getattr(draft, "activity_type", "assign") == "quiz" or (
            draft.structured_answers is not None and len(draft.structured_answers) > 0
        )

        current_md = draft.full_markdown or ""
        if not current_md and getattr(draft, "output_path", None) and Path(draft.output_path).exists():
            try:
                current_md = Path(draft.output_path).read_text(encoding="utf-8", errors="ignore")
            except Exception:
                pass

        system_instruction = (
            "Você é um assistente acadêmico especialista de alta precisão. O estudante analisou a resolução do trabalho/questionário "
            "e solicitou modificações pontuais no arquivo final gerado.\n"
            "Mantenha rigorosamente o formato acadêmico estruturado em Markdown com títulos '### Questão X'. "
            "Aplique EXCLUSIVAMENTE as modificações e correções solicitadas pelo aluno, preservando com fidelidade "
            "o restante do conteúdo e a formatação que não foram pedidos para alterar.\n"
        )
        if is_quiz:
            system_instruction += (
                "IMPORTANTE: Como se trata de um questionário/quiz, ao final de sua resposta inclua obrigatoriamente um bloco "
                "```json:answers contendo o JSON com a lista de respostas atualizadas no formato:\n"
                '[{"key": "q1", "value": "A"}, {"key": "q2", "value": "C"}, ...]\n```'
            )

        user_message = (
            f"ARQUIVO / RESOLUÇÃO ANTERIOR DO ALUNO:\n{current_md}\n\n"
            f"MODIFICAÇÕES SOLICITADAS PELO ALUNO:\n{revision_instructions}\n\n"
            "Reescreva e gere a resolução completa com as modificações aplicadas com precisão."
        )

        last_error = None

        for idx, prov in enumerate(self.chain):
            is_ready, reason = self._is_provider_ready(prov)
            if not is_ready:
                continue

            try:
                if prov == "gemini" and self._gemini_solver and self._gemini_solver.client:
                    return await self._gemini_solver.apply_revision(
                        draft=draft,
                        revision_instructions=revision_instructions,
                        on_log=on_log,
                    )
                elif prov == "anthropic":
                    full_text, used_model = await _call_claude(system_instruction, user_message, on_log)
                elif prov == "deepseek":
                    full_text, used_model = await _call_deepseek(system_instruction, user_message, on_log)
                else:
                    full_text, used_model = await self.generate_text(system_instruction, user_message, on_log=on_log)

                from src.scraper.moodle_scraper import Assignment as _Assign
                mock_assign = _Assign(
                    id=draft.assignment_id,
                    course_id="",
                    course_name=draft.course_name,
                    title=draft.assignment_title,
                    url="",
                    description="",
                    activity_type="quiz" if is_quiz else "assign",
                )
                res_draft = await _build_solution_draft(
                    assignment=mock_assign,
                    full_text=full_text,
                    used_model=used_model,
                    on_log=on_log,
                    auto_triggered=draft.auto_triggered,
                    is_quiz=is_quiz,
                    used_materials=getattr(draft, "used_materials", []),
                )
                if not res_draft.structured_answers and draft.structured_answers:
                    res_draft.structured_answers = draft.structured_answers
                return res_draft

            except Exception as err:
                last_error = err
                if idx < len(self.chain) - 1:
                    next_prov = self.chain[idx + 1]
                    await _emit_log(on_log, f"⚠️ Falha na revisão via {prov}. Alternando para: {next_prov}...")

        raise RuntimeError(f"Falha ao aplicar revisão em todos os provedores: {last_error}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _build_solution_draft(
    assignment: Assignment,
    full_text: str,
    used_model: str,
    on_log: Optional[Any],
    auto_triggered: bool,
    is_quiz: bool = False,
    used_materials: Optional[List[str]] = None,
):
    """Constrói SolutionDraft a partir do texto bruto da IA, gerando PDF ou DOCX conforme contexto."""
    import json as _json
    from src.scraper.moodle_scraper import sanitize_filename
    from src.solver.gemini_solver import SolutionDraft

    # Extrai JSON:answers de forma robusta (com ou sem crases)
    structured_answers = None
    json_match = re.search(r"```(?:json:answers|json)?\s*([\s\S]*?)\s*```", full_text)
    candidate_json = None
    if json_match:
        cand = json_match.group(1).strip()
        if (cand.startswith("{") and cand.endswith("}")) or (cand.startswith("[") and cand.endswith("]")):
            candidate_json = cand
    if not candidate_json:
        raw_m = re.search(r"(?:json:answers|json)?\s*(\{[\s\S]*\}|\[[\s\S]*\])", full_text)
        if raw_m:
            candidate_json = raw_m.group(1).strip()

    if candidate_json:
        try:
            parsed = _json.loads(candidate_json)
            if isinstance(parsed, list):
                structured_answers = parsed
            elif isinstance(parsed, dict):
                structured_answers = [{"key": k, "value": v} for k, v in parsed.items()]
        except Exception:
            pass

    # Limpeza do markdown: remove bloco json e resíduos
    clean_markdown = re.sub(r"```(?:json:answers|json)\s*\n.*?\n```", "", full_text, flags=re.DOTALL).strip()
    clean_markdown = re.sub(r"```json\s*[\s\S]*?```", "", clean_markdown, flags=re.IGNORECASE).strip()
    clean_markdown = re.sub(r"^json:answers\s*\{[\s\S]*?\}", "", clean_markdown, flags=re.IGNORECASE).strip()
    clean_markdown = re.sub(r"^json:answers\s*", "", clean_markdown, flags=re.IGNORECASE).strip()
    clean_markdown = re.sub(r"(?i)texto (?:informativo|da questão)", "", clean_markdown)
    clean_markdown = re.sub(r"(?i)resposta \d+\s*questão \d+", "", clean_markdown)
    clean_markdown = re.sub(r"(?i)verificar questão \d+", "", clean_markdown)
    clean_markdown = re.sub(r"### Informação\s*\n.*?(?=### Questão|\Z)", "", clean_markdown, flags=re.DOTALL)
    clean_markdown = re.sub(r"\n{3,}", "\n\n", clean_markdown).strip()

    # Se a IA respondeu apenas JSON e clean_markdown ficou vazio, reconstrói Folha de Respostas
    if not clean_markdown.strip() and structured_answers:
        reconstructed = ["### Folha de Respostas\n"]
        for idx, item in enumerate(structured_answers, 1):
            k = item.get("key") or item.get("field") or f"Questão {idx}"
            v = item.get("value", "")
            if re.match(r"^Q\d+$", str(k), re.IGNORECASE):
                q_num = re.sub(r"\D", "", str(k))
                reconstructed.append(f"### Questão {q_num}\n- **Resposta:** {v}\n")
            elif str(k).upper().startswith("CAMPO_"):
                reconstructed.append(f"- **{k}:** {v}\n")
            else:
                reconstructed.append(f"### {k}\n- **Resposta:** {v}\n")
        clean_markdown = "\n".join(reconstructed).strip()

    # Prepara o resumo sem ruídos e garantindo que não fique em branco
    summary_lines = [l for l in clean_markdown.splitlines() if l.strip() and not l.startswith("#")]
    if summary_lines:
        summary = "\n".join(summary_lines[:8])
    elif structured_answers:
        summary = "\n".join(
            f"• {it.get('key') or it.get('field') or 'Q'}: {it.get('value', '')}"
            for it in structured_answers[:8]
        )
    else:
        summary = clean_markdown[:400] if clean_markdown else "Resolução concluída com sucesso."

    safe_course = sanitize_filename(assignment.course_name)
    safe_title = sanitize_filename(assignment.title)
    dest_dir = settings.STORAGE_SUBMISSIONS_DIR / safe_course
    dest_dir.mkdir(parents=True, exist_ok=True)

    draft_path = dest_dir / f"{safe_title}_rascunho.md"
    draft_path.write_text(clean_markdown, encoding="utf-8")

    pdf_path = None
    docx_path = None

    if auto_triggered:
        # Resolução automática → gera PDF
        pdf_path = dest_dir / f"{safe_title}.pdf"
        try:
            await _emit_log(on_log, "Compilando PDF acadêmico da resolução...")
            from src.solver.pdf_generator import AcademicPDFGenerator
            pdf_gen = AcademicPDFGenerator()
            await pdf_gen.render_pdf(
                markdown_text=clean_markdown,
                output_pdf_path=pdf_path,
                course_name=assignment.course_name,
                assignment_title=assignment.title,
            )
            await _emit_log(on_log, f"✔ PDF gerado: {pdf_path.name}")
        except Exception as e:
            console.print(f"[yellow]Aviso ao gerar PDF: {e}[/yellow]")
            pdf_path = None
    else:
        # Resolução manual → gera DOCX editável
        docx_path = dest_dir / f"{safe_title}.docx"
        try:
            await _emit_log(on_log, "Gerando documento Word editável (.docx)...")
            from src.solver.docx_generator import AcademicDocxGenerator
            docx_gen = AcademicDocxGenerator()
            ok = docx_gen.generate_docx(
                markdown_text=clean_markdown,
                output_path=docx_path,
                course_name=assignment.course_name,
                assignment_title=assignment.title,
            )
            if ok:
                await _emit_log(on_log, f"✔ DOCX editável gerado: {docx_path.name}")
            else:
                docx_path = None
        except Exception as e:
            console.print(f"[yellow]Aviso ao gerar DOCX: {e}[/yellow]")
            docx_path = None

    return SolutionDraft(
        assignment_id=assignment.id,
        assignment_title=assignment.title,
        course_name=assignment.course_name,
        summary=summary,
        full_markdown=full_text,
        output_path=draft_path,
        pdf_path=pdf_path,
        docx_path=docx_path,
        used_materials=used_materials or [],
        used_model=used_model,
        structured_answers=structured_answers,
        activity_type="quiz" if is_quiz else getattr(assignment, "activity_type", "assign"),
    )
