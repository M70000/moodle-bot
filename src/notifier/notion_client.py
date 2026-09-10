"""Módulo de Integração com o Notion e Central de Estudos.

Gerencia a sincronização automática de tarefas, listas de exercícios, prazos e rotinas
com a Central de Estudos do Notion do estudante. Sempre que qualquer item é adicionado,
notifica detalhadamente o canal de anúncios do Discord.
"""

import asyncio
import json
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import httpx
from rich.console import Console

from config.settings import settings

if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console()


class NotionClient:
    """Cliente para a API oficial do Notion (versão 2022-06-28)."""

    def __init__(
        self,
        token: Optional[str] = None,
        tasks_db_id: Optional[str] = None,
        courses_db_id: Optional[str] = None,
        page_id: Optional[str] = None,
        checklist_block_id: Optional[str] = None,
        schedule_table_id: Optional[str] = None
    ):
        self.token = token if token is not None else settings.NOTION_API_KEY
        self.tasks_db_id = tasks_db_id if tasks_db_id is not None else settings.NOTION_TASKS_DATABASE_ID
        self.courses_db_id = courses_db_id if courses_db_id is not None else settings.NOTION_COURSES_DATABASE_ID
        self.page_id = page_id if page_id is not None else settings.NOTION_PAGE_ID
        self.checklist_block_id = checklist_block_id if checklist_block_id is not None else settings.NOTION_DAILY_CHECKLIST_BLOCK_ID
        self.schedule_table_id = schedule_table_id if schedule_table_id is not None else settings.NOTION_WEEKLY_SCHEDULE_TABLE_ID
        self._courses_cache: Dict[str, str] = {}
        self._courses_loaded = False

    @property
    def is_configured(self) -> bool:
        """Verifica se o token e as databases do Notion estão configurados."""
        return bool(self.token and self.token.startswith("ntn_"))

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"
        }

    async def load_courses(self) -> Dict[str, str]:
        """Carrega e mapeia cursos/disciplinas cadastrados no Notion para IDs de relação."""
        if not self.is_configured or not self.courses_db_id:
            return {}

        if self._courses_loaded:
            return self._courses_cache

        url = f"https://api.notion.com/v1/databases/{self.courses_db_id}/query"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(url, headers=self._get_headers(), json={"page_size": 50})
                if res.status_code == 200:
                    data = res.json()
                    for item in data.get("results", []):
                        cid = item.get("id")
                        props = item.get("properties", {})
                        name_list = props.get("Nome", {}).get("title", [])
                        c_name = "".join([t.get("plain_text", "") for t in name_list]).strip()
                        code_list = props.get("course code", {}).get("rich_text", [])
                        c_code = "".join([t.get("plain_text", "") for t in code_list]).strip()
                        abbrev_list = props.get("abreviation", {}).get("rich_text", [])
                        c_abbrev = "".join([t.get("plain_text", "") for t in abbrev_list]).strip()

                        if c_name:
                            self._courses_cache[c_name.lower()] = cid
                        if c_code:
                            self._courses_cache[c_code.lower()] = cid
                        if c_abbrev:
                            self._courses_cache[c_abbrev.lower()] = cid

                    self._courses_loaded = True
        except Exception as e:
            console.print(f"[yellow]Aviso: Não foi possível carregar mapeamento de cursos do Notion: {e}[/yellow]")

        return self._courses_cache

    async def resolve_course_id(self, course_name: Optional[str]) -> Optional[str]:
        """Localiza o ID da página da disciplina no Notion por correspondência flexível."""
        if not course_name:
            return None

        courses = await self.load_courses()
        c_lower = course_name.lower().strip()

        # Correspondência exata
        if c_lower in courses:
            return courses[c_lower]

        # Correspondência parcial (ex: "eletromagnetismo" em "Física - Eletromagnetismo")
        for key, cid in courses.items():
            if key in c_lower or c_lower in key:
                return cid

        # Fallback padrão para Eletromagnetismo se contiver "eletromag"
        if "eletromag" in c_lower:
            return "d6266a51-585d-48ba-a9a5-84cb7cc55ca1"

        return None

    async def find_task_by_id(self, task_id_val: str) -> Optional[Dict[str, Any]]:
        """Verifica se uma tarefa com o Task ID fornecido já existe no Notion."""
        if not self.is_configured or not self.tasks_db_id or not task_id_val:
            return None

        url = f"https://api.notion.com/v1/databases/{self.tasks_db_id}/query"
        payload = {
            "filter": {
                "property": "Task ID",
                "rich_text": {
                    "equals": task_id_val
                }
            }
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(url, headers=self._get_headers(), json=payload)
                if res.status_code == 200:
                    results = res.json().get("results", [])
                    if results:
                        return results[0]
        except Exception:
            pass
        return None

    async def create_task(
        self,
        title: str,
        date_str: Optional[str] = None,
        category: str = "TAREFA✅",
        course_name: Optional[str] = None,
        task_id_val: Optional[str] = None,
        notes_val: Optional[str] = None,
        details: Optional[str] = None,
        steps: Optional[List[str]] = None,
        status: str = "não iniciado",
        domain: str = "à fazer",
        moodle_url: Optional[str] = None,
        notify_discord: bool = True
    ) -> Dict[str, Any]:
        """Cria uma tarefa na database 'à fazer' do Notion e notifica o canal de anúncios."""
        if not self.is_configured:
            return {"success": False, "error": "Notion não configurado (.env ausente ou inválido)"}

        if not self.tasks_db_id:
            return {"success": False, "error": "NOTION_TASKS_DATABASE_ID não configurado"}

        # Não adiciona atividades do Moodle que não possuem prazo/data definida
        if task_id_val and task_id_val.startswith("moodle_") and not date_str:
            console.print(f"[dim]Atividade do Moodle '{title}' sem prazo de entrega. Ignorando sincronização com Notion.[/dim]")
            return {"success": False, "skipped": True, "error": "Atividade do Moodle sem prazo de entrega"}

        # Evita duplicidade se o Task ID já existir
        if task_id_val:
            existing = await self.find_task_by_id(task_id_val)
            if existing:
                page_id = existing.get("id", "")
                page_url = f"https://www.notion.so/{page_id.replace('-', '')}"
                console.print(f"[dim]Tarefa '{title}' já existe no Notion (ID: {page_id}). Pulando criação.[/dim]")
                return {"success": True, "already_exists": True, "page_id": page_id, "url": page_url}

        course_rel_id = await self.resolve_course_id(course_name)

        # Constrói os blocos internos da página no Notion
        page_children: List[Dict[str, Any]] = []

        # 1. Callout de resumo com emoji
        callout_text = details or f"Material sincronizado pelo Assistente Acadêmico do Moodle."
        if moodle_url:
            callout_text += f"\nLink Original: {moodle_url}"

        page_children.append({
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content": callout_text[:1900]}}],
                "icon": {"emoji": "📚" if "estudo" in title.lower() or "lista" in title.lower() else "📝"}
            }
        })

        # 2. Checklist de etapas de estudo/execução
        actual_steps = steps or [
            f"Revisar conceitos e materiais da matéria {course_name or ''}".strip(),
            "Resolver os exercícios propostos",
            "Conferir respostas e gabarito antes da entrega"
        ]

        if actual_steps:
            page_children.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {
                    "rich_text": [{"type": "text", "text": {"content": "Etapas de Estudo e Execução"}}]
                }
            })
            for step in actual_steps:
                page_children.append({
                    "object": "block",
                    "type": "to_do",
                    "to_do": {
                        "rich_text": [{"type": "text", "text": {"content": str(step)[:1900]}}],
                        "checked": False
                    }
                })

        # Monta propriedades da Database do Notion
        properties: Dict[str, Any] = {
            "Task Title": {
                "title": [{"type": "text", "text": {"content": title}}]
            },
            "tarefa": {
                "select": {"name": category}
            },
            "progresso": {
                "status": {"name": status}
            },
            "domínio": {
                "status": {"name": domain}
            }
        }

        if date_str:
            properties["data"] = {
                "date": {"start": date_str}
            }

        if course_rel_id:
            properties["curso"] = {
                "relation": [{"id": course_rel_id}]
            }

        if notes_val:
            properties["notas"] = {
                "rich_text": [{"type": "text", "text": {"content": notes_val[:1900]}}]
            }

        if task_id_val:
            properties["Task ID"] = {
                "rich_text": [{"type": "text", "text": {"content": task_id_val[:500]}}]
            }

        payload = {
            "parent": {"database_id": self.tasks_db_id},
            "properties": properties,
            "children": page_children
        }

        url = "https://api.notion.com/v1/pages"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                res = await client.post(url, headers=self._get_headers(), json=payload)
                if res.status_code in [200, 201]:
                    data = res.json()
                    page_id = data.get("id", "")
                    page_url = f"https://www.notion.so/{page_id.replace('-', '')}"
                    console.print(f"[bold green]✔ Tarefa adicionada ao Notion com sucesso:[/bold green] {title} ({page_url})")

                    # NOTIFICAÇÃO OBRIGATÓRIA NO CANAL DE ANÚNCIOS DO DISCORD
                    if notify_discord:
                        await self._notify_discord_announcement(
                            title=title,
                            item_type=category,
                            course_name=course_name,
                            date_str=date_str,
                            notion_url=page_url,
                            moodle_url=moodle_url,
                            details=details or notes_val or "Atividade adicionada ao planejamento da sua central de estudos.",
                            steps=actual_steps,
                            notes=notes_val,
                            status=status,
                            action="Nova Atividade no Notion"
                        )

                    return {
                        "success": True,
                        "page_id": page_id,
                        "url": page_url
                    }
                else:
                    err_msg = res.text
                    console.print(f"[red]Erro HTTP {res.status_code} ao criar tarefa no Notion: {err_msg}[/red]")
                    return {"success": False, "error": f"Erro HTTP {res.status_code}: {err_msg}"}
        except Exception as e:
            console.print(f"[red]Erro na chamada da API do Notion: {e}[/red]")
            return {"success": False, "error": str(e)}

    async def create_general_item(
        self,
        title: str,
        content: str,
        item_type: str = "Anotação / Resumo",
        course_name: Optional[str] = None,
        date_str: Optional[str] = None,
        steps: Optional[List[str]] = None,
        notify_discord: bool = True
    ) -> Dict[str, Any]:
        """Cria uma página geral ou anotação no Notion e notifica o canal de anúncios com detalhes."""
        if not self.is_configured:
            return {"success": False, "error": "Notion não configurado"}

        # Se tiver tasks_db_id configurada, cria como item do tipo OUTROS
        if self.tasks_db_id:
            return await self.create_task(
                title=title,
                date_str=date_str or datetime.now().strftime("%Y-%m-%d"),
                category="OUTROS",
                course_name=course_name,
                details=content,
                notes_val=content[:200],
                steps=steps,
                notify_discord=notify_discord
            )

        # Fallback: cria como subpágina da página principal
        parent_id = self.page_id
        if not parent_id:
            return {"success": False, "error": "Nenhum parent_id (page_id ou tasks_db_id) configurado no Notion"}

        payload = {
            "parent": {"page_id": parent_id},
            "properties": {
                "title": {
                    "title": [{"type": "text", "text": {"content": title}}]
                }
            },
            "children": [
                {
                    "object": "block",
                    "type": "callout",
                    "callout": {
                        "rich_text": [{"type": "text", "text": {"content": content[:1900]}}],
                        "icon": {"emoji": "📌"}
                    }
                }
            ]
        }

        url = "https://api.notion.com/v1/pages"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                res = await client.post(url, headers=self._get_headers(), json=payload)
                if res.status_code in [200, 201]:
                    data = res.json()
                    page_id = data.get("id", "")
                    page_url = f"https://www.notion.so/{page_id.replace('-', '')}"

                    if notify_discord:
                        await self._notify_discord_announcement(
                            title=title,
                            item_type=item_type,
                            course_name=course_name,
                            date_str=date_str,
                            notion_url=page_url,
                            details=content,
                            steps=steps,
                            action="Novo Conteúdo no Notion"
                        )

                    return {"success": True, "page_id": page_id, "url": page_url}
                else:
                    return {"success": False, "error": res.text}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_daily_routine_schedule(self, target_date: Optional[datetime] = None) -> List[str]:
        """Lê a tabela 'Agenda Semanal' no Notion e extrai somente as tarefas e blocos de estudo do dia (sem aulas, lazer ou academia)."""
        if not self.is_configured or not self.schedule_table_id:
            return []

        dt = target_date or datetime.now()
        dias_semana = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
        dia_nome = dias_semana[dt.weekday()]

        url = f"https://api.notion.com/v1/blocks/{self.schedule_table_id}/children?page_size=100"
        routine_items: List[str] = []
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.get(url, headers=self._get_headers())
                if res.status_code == 200:
                    results = res.json().get("results", [])
                    if not results:
                        return []

                    header_cells = results[0].get("table_row", {}).get("cells", [])
                    headers = ["".join([x.get("plain_text", "") for x in c]).lower().strip() for c in header_cells]
                    col_idx = None
                    for i, h in enumerate(headers):
                        if dia_nome in h:
                            col_idx = i
                            break

                    if col_idx is not None:
                        for row in results[1:]:
                            cells = row.get("table_row", {}).get("cells", [])
                            if len(cells) > col_idx:
                                act = "".join([x.get("plain_text", "") for x in cells[col_idx]]).strip()
                                act_lower = act.lower()

                                # Identifica blocos ativos de estudo e tarefas práticas
                                is_study = any(kw in act_lower for kw in [
                                    "estudar", "revis", "leitura", "exerc", "lista", "praticar", "planejamento"
                                ])
                                # Ignora estritamente: horários de aula, lazer, refeições, sono, academia e rotinas pessoais
                                is_ignored = any(ign in act_lower for ign in [
                                    "dormir", "banho", "café", "cafe", "almoço", "almoco", "jantar", "lanche",
                                    "pós-treino", "pos-treino", "lazer", "descanso", "academia", "treino",
                                    "aula", "online", "geral", "fund.", "exp.", "estatística", "estatistica", "instrumental"
                                ])

                                if is_study and not is_ignored:
                                    if act_lower.startswith("revisão "):
                                        clean_act = "Revisar " + act[8:]
                                    elif act_lower.startswith("revisao "):
                                        clean_act = "Revisar " + act[8:]
                                    else:
                                        clean_act = act

                                    if clean_act not in routine_items:
                                        routine_items.append(clean_act)
        except Exception as e:
            console.print(f"[yellow]Aviso ao buscar rotina de estudos no Notion: {e}[/yellow]")

        return routine_items

    async def get_priority_tasks(self, target_date: Optional[datetime] = None) -> List[str]:
        """Consulta tarefas pendentes na database do Notion marcadas estritamente para o dia de hoje (sem atrasados ou amanhã)."""
        if not self.is_configured or not self.tasks_db_id:
            return []

        dt = target_date or datetime.now()
        today_date = dt.date()
        today_str = today_date.strftime("%Y-%m-%d")

        url = f"https://api.notion.com/v1/databases/{self.tasks_db_id}/query"
        payload = {
            "filter": {
                "and": [
                    {
                        "property": "progresso",
                        "status": {"does_not_equal": "completo"}
                    },
                    {
                        "property": "data",
                        "date": {"equals": today_str}
                    }
                ]
            },
            "page_size": 50
        }

        today_items: List[str] = []
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(url, headers=self._get_headers(), json=payload)
                if res.status_code == 200:
                    for t in res.json().get("results", []):
                        props = t.get("properties", {})
                        title_list = props.get("Task Title", {}).get("title", [])
                        raw_title = "".join([x.get("plain_text", "") for x in title_list]).strip()
                        if not raw_title:
                            continue

                        # Formatação direta para execução (ex: "Lista de Estudo 2" -> "Fazer Lista de Estudo 2")
                        raw_lower = raw_title.lower()
                        if raw_lower.startswith("lista ") or raw_lower.startswith("exercício") or raw_lower.startswith("exercicio"):
                            clean_title = f"Fazer {raw_title}"
                        elif raw_lower.startswith("atividade ") or raw_lower.startswith("trabalho "):
                            clean_title = f"Fazer {raw_title}"
                        else:
                            clean_title = raw_title

                        if clean_title not in today_items:
                            today_items.append(clean_title)
        except Exception as e:
            console.print(f"[yellow]Aviso ao buscar tarefas do dia no Notion: {e}[/yellow]")

        return today_items

    async def update_daily_checklist(
        self,
        target_date: Optional[datetime] = None,
        custom_tasks: Optional[List[str]] = None,
        notify_discord: bool = True
    ) -> Dict[str, Any]:
        """Atualiza a checklist diária no bloco toggle 'tarefas do dia' no Notion.

        Remove estritamente os blocos 'to_do' anteriores, preservando intacto o botão
        interativo 'e aí?' (bloco unsupported) e quaisquer outros elementos fixos.
        Em seguida, insere as tarefas e compromissos acadêmicos do dia.
        """
        if not self.is_configured:
            return {"success": False, "error": "Notion não configurado"}

        if not self.checklist_block_id:
            return {"success": False, "error": "NOTION_DAILY_CHECKLIST_BLOCK_ID não configurado"}

        dt = target_date or datetime.now()
        today_str = dt.strftime("%Y-%m-%d")
        dias_semana = ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira", "Sexta-feira", "Sábado", "Domingo"]
        dia_nome = dias_semana[dt.weekday()]

        console.print(f"[cyan]Atualizando checklist diária do Notion para {today_str} ({dia_nome})...[/cyan]")

        # 1. Busca os blocos filhos atuais do bloco toggle
        url_children = f"https://api.notion.com/v1/blocks/{self.checklist_block_id}/children?page_size=100"
        async with httpx.AsyncClient(timeout=20.0) as client:
            res = await client.get(url_children, headers=self._get_headers())
            if res.status_code != 200:
                err_msg = f"Erro ao acessar bloco de checklist no Notion: {res.status_code} {res.text}"
                console.print(f"[red]{err_msg}[/red]")
                return {"success": False, "error": err_msg}

            current_blocks = res.json().get("results", [])

            # 2. Identifica e deleta APENAS blocos to_do, NUNCA o botão interativo ou outros blocos
            to_do_block_ids = [
                b.get("id") for b in current_blocks if b.get("type") == "to_do"
            ]

            for b_id in to_do_block_ids:
                try:
                    await client.delete(f"https://api.notion.com/v1/blocks/{b_id}", headers=self._get_headers())
                except Exception as del_err:
                    console.print(f"[yellow]Aviso ao remover to_do antigo {b_id}: {del_err}[/yellow]")

            # 3. Compila a lista de tarefas do dia
            if custom_tasks is not None:
                final_tasks = custom_tasks
            else:
                priority_tasks = await self.get_priority_tasks(dt)
                routine_tasks = await self.get_daily_routine_schedule(dt)
                final_tasks = []
                for pt in priority_tasks:
                    if pt not in final_tasks:
                        final_tasks.append(pt)
                for rt in routine_tasks:
                    if rt not in final_tasks:
                        final_tasks.append(rt)

            if not final_tasks:
                final_tasks = ["Nenhuma pendência imediata para hoje! Revisar matérias ou adiantar próximas entregas 🚀"]

            # 4. Adiciona os novos blocos to_do ao bloco toggle
            new_blocks = [
                {
                    "object": "block",
                    "type": "to_do",
                    "to_do": {
                        "rich_text": [{"type": "text", "text": {"content": str(task_txt)[:1900]}}],
                        "checked": False
                    }
                }
                for task_txt in final_tasks
            ]

            patch_url = f"https://api.notion.com/v1/blocks/{self.checklist_block_id}/children"
            res_patch = await client.patch(patch_url, headers=self._get_headers(), json={"children": new_blocks})
            if res_patch.status_code not in [200, 201]:
                err_msg = f"Erro ao adicionar novos to_do no Notion: {res_patch.status_code} {res_patch.text}"
                console.print(f"[red]{err_msg}[/red]")
                return {"success": False, "error": err_msg}

            console.print(f"[bold green]✔ Checklist do dia atualizada no Notion com {len(final_tasks)} tarefas![/bold green]")

            # 5. Notificação no Discord (se solicitado)
            if notify_discord:
                page_url = f"https://www.notion.so/{self.page_id.replace('-', '')}" if self.page_id else None
                try:
                    from src.notifier.discord_bot import MoodleDiscordNotifier
                    notifier = MoodleDiscordNotifier()
                    await notifier.send_daily_checklist_announcement(
                        day_name=dia_nome,
                        date_str=today_str,
                        checklist_items=final_tasks,
                        notion_url=page_url
                    )
                except Exception as disc_err:
                    console.print(f"[yellow]Aviso ao enviar notificação de checklist no Discord: {disc_err}[/yellow]")

            return {
                "success": True,
                "date": today_str,
                "day_name": dia_nome,
                "tasks_count": len(final_tasks),
                "tasks": final_tasks
            }

    async def _notify_discord_announcement(
        self,
        title: str,
        item_type: str = "Atividade / Tarefa",
        course_name: Optional[str] = None,
        date_str: Optional[str] = None,
        notion_url: Optional[str] = None,
        moodle_url: Optional[str] = None,
        details: Optional[str] = None,
        steps: Optional[List[str]] = None,
        notes: Optional[str] = None,
        status: Optional[str] = "não iniciado",
        action: str = "Nova Atividade Registrada no Notion"
    ):
        """Dispara a notificação detalhada para o canal de avisos do Discord."""
        try:
            from src.notifier.discord_bot import MoodleDiscordNotifier
            notifier = MoodleDiscordNotifier()
            await notifier.send_notion_announcement(
                title=title,
                item_type=item_type,
                course_name=course_name,
                date_str=date_str,
                notion_url=notion_url,
                moodle_url=moodle_url,
                details=details,
                steps=steps,
                notes=notes,
                status=status,
                action=action
            )
        except Exception as err:
            console.print(f"[yellow]Aviso: Não foi possível enviar notificação ao Discord: {err}[/yellow]")


# Instância singleton padrão do NotionClient
notion_client = NotionClient()
