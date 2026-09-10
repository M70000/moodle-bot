"""Testes unitários para o cliente Notion e anúncios no Discord."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from src.notifier.notion_client import NotionClient
from src.notifier.discord_bot import MoodleDiscordNotifier


class TestNotionIntegration(unittest.IsolatedAsyncioTestCase):
    """Bateria de testes para NotionClient e notificações de anúncios."""

    def setUp(self):
        self.client = NotionClient(
            token="ntn_test_token_12345",
            tasks_db_id="test_tasks_db_id",
            courses_db_id="test_courses_db_id",
            page_id="test_page_id"
        )

    def test_client_configuration(self):
        self.assertTrue(self.client.is_configured)
        unconfigured = NotionClient(token="")
        self.assertFalse(unconfigured.is_configured)

    async def test_resolve_course_fallback(self):
        self.client._courses_cache = {
            "eletromagnetismo": "rel_id_eletromag",
            "calculo 1": "rel_id_calc1"
        }
        self.client._courses_loaded = True

        cid1 = await self.client.resolve_course_id("Eletromagnetismo")
        self.assertEqual(cid1, "rel_id_eletromag")

        cid2 = await self.client.resolve_course_id("Física - Eletromagnetismo Teórico")
        self.assertEqual(cid2, "rel_id_eletromag")

        cid3 = await self.client.resolve_course_id("Desconhecida")
        self.assertIsNone(cid3)

    @patch("httpx.AsyncClient.post")
    async def test_create_task_calls_discord_announcement(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "12345678-abcd-ef01-2345-6789abcdef01"
        }
        mock_post.return_value = mock_resp

        with patch.object(self.client, "_notify_discord_announcement", new_callable=AsyncMock) as mock_notify:
            with patch.object(self.client, "find_task_by_id", new_callable=AsyncMock) as mock_find:
                mock_find.return_value = None

                res = await self.client.create_task(
                    title="Lista de Teste 10",
                    date_str="2026-09-20",
                    category="TAREFA✅",
                    course_name="Eletromagnetismo",
                    details="Exercícios sobre leis de Ampere",
                    steps=["Passo 1", "Passo 2"],
                    notify_discord=True
                )

                self.assertTrue(res.get("success"))
                self.assertIn("notion.so", res.get("url", ""))
                mock_notify.assert_awaited_once()
                args, kwargs = mock_notify.call_args
                self.assertEqual(kwargs.get("title"), "Lista de Teste 10")
                self.assertEqual(kwargs.get("item_type"), "TAREFA✅")
                self.assertEqual(kwargs.get("course_name"), "Eletromagnetismo")

    async def test_create_task_skips_undated_moodle_assignment(self):
        with patch.object(self.client, "_notify_discord_announcement", new_callable=AsyncMock) as mock_notify:
            res = await self.client.create_task(
                title="Unidade 5 :: Aula 1",
                date_str=None,  # Sem data de entrega
                category="TRABALHO🟡",
                course_name="Inglês Instrumental",
                task_id_val="moodle_quiz_9999",
                notify_discord=True
            )
            self.assertFalse(res.get("success"))
            self.assertTrue(res.get("skipped"))
            mock_notify.assert_not_called()

    async def test_send_notion_announcement_format(self):
        notifier = MoodleDiscordNotifier(token="test_token", channel_id=123456)
        mock_channel = MagicMock()
        mock_channel.send = AsyncMock()

        with patch.object(notifier, "_resolve_channel", new_callable=AsyncMock) as mock_resolve:
            mock_resolve.return_value = mock_channel

            success = await notifier.send_notion_announcement(
                title="Lista 9 - Circuitos Magnéticos",
                item_type="TAREFA✅",
                course_name="Eletromagnetismo",
                date_str="2026-09-18",
                notion_url="https://notion.so/test12345",
                details="Lista prática de exercícios para fixação.",
                steps=["Revisar teoria", "Resolver exercícios 1 a 5"],
                status="não iniciado"
            )

            self.assertTrue(success)
            mock_channel.send.assert_awaited_once()
            _, send_kwargs = mock_channel.send.call_args
            embed = send_kwargs.get("embed")
            self.assertIsNotNone(embed)
            self.assertIn("Lista 9 - Circuitos Magnéticos", embed.title)
            field_names = [f.name for f in embed.fields]
            self.assertIn("📌 Categoria / Tipo", field_names)
            self.assertIn("🏫 Disciplina", field_names)
            self.assertIn("📅 Data / Prazo", field_names)
            self.assertIn("📋 Etapas de Estudo Planejadas", field_names)

    @patch("httpx.AsyncClient.get")
    async def test_get_daily_routine_schedule(self, mock_get):
        from datetime import datetime
        # Fixa uma data de quinta-feira: 2026-09-10
        dt = datetime(2026, 9, 10, 10, 0, 0)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [
                {
                    "table_row": {
                        "cells": [
                            [{"plain_text": "Horário"}],
                            [{"plain_text": "Segunda-feira"}],
                            [{"plain_text": "Terça-feira"}],
                            [{"plain_text": "Quarta-feira"}],
                            [{"plain_text": "Quinta-feira"}],
                            [{"plain_text": "Sexta-feira"}],
                            [{"plain_text": "Sábado"}],
                            [{"plain_text": "Domingo"}]
                        ]
                    }
                },
                {
                    "table_row": {
                        "cells": [
                            [{"plain_text": "08:00 - 10:00"}],
                            [{"plain_text": "Química Geral B"}],
                            [{"plain_text": "Cálculo"}],
                            [{"plain_text": "Física"}],
                            [{"plain_text": "Química Geral B"}],  # Aula -> deve ser ignorada
                            [{"plain_text": "Estatística"}],
                            [{"plain_text": ""}],
                            [{"plain_text": ""}]
                        ]
                    }
                },
                {
                    "table_row": {
                        "cells": [
                            [{"plain_text": "12:00 - 13:00"}],
                            [{"plain_text": "Almoço"}],
                            [{"plain_text": "Almoço"}],
                            [{"plain_text": "Almoço"}],
                            [{"plain_text": "Almoço"}],  # Almoço -> deve ser ignorado
                            [{"plain_text": "Almoço"}],
                            [{"plain_text": ""}],
                            [{"plain_text": ""}]
                        ]
                    }
                },
                {
                    "table_row": {
                        "cells": [
                            [{"plain_text": "15:30 - 16:30"}],
                            [{"plain_text": "Estudar Química"}],
                            [{"plain_text": "Estudar Química"}],
                            [{"plain_text": "Estudar Química"}],
                            [{"plain_text": "Estudar Química"}],  # Estudo ativo -> DEVE SER INCLUÍDO
                            [{"plain_text": ""}],
                            [{"plain_text": ""}],
                            [{"plain_text": ""}]
                        ]
                    }
                },
                {
                    "table_row": {
                        "cells": [
                            [{"plain_text": "17:00 - 18:30"}],
                            [{"plain_text": "Academia"}],
                            [{"plain_text": "Academia"}],
                            [{"plain_text": "Academia"}],
                            [{"plain_text": "Academia"}],  # Academia -> deve ser ignorada
                            [{"plain_text": "Lazer"}],
                            [{"plain_text": ""}],
                            [{"plain_text": ""}]
                        ]
                    }
                }
            ]
        }
        mock_get.return_value = mock_resp

        items = await self.client.get_daily_routine_schedule(target_date=dt)
        self.assertEqual(len(items), 1)
        self.assertIn("Estudar Química", items)
        # Aula, Almoço e Academia devem ser filtrados
        self.assertFalse(any("Química Geral" in it for it in items))
        self.assertFalse(any("Almoço" in it for it in items))
        self.assertFalse(any("Academia" in it for it in items))

    @patch("httpx.AsyncClient.post")
    async def test_get_priority_tasks_today_only(self, mock_post):
        from datetime import datetime
        dt = datetime(2026, 9, 10, 10, 0, 0)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [
                {
                    "properties": {
                        "Task Title": {"title": [{"plain_text": "Lista de Estudo 2"}]},
                        "data": {"date": {"start": "2026-09-10"}}
                    }
                },
                {
                    "properties": {
                        "Task Title": {"title": [{"plain_text": "Revisar Anotações"}]},
                        "data": {"date": {"start": "2026-09-10"}}
                    }
                }
            ]
        }
        mock_post.return_value = mock_resp

        tasks = await self.client.get_priority_tasks(target_date=dt)
        self.assertEqual(len(tasks), 2)
        self.assertIn("Fazer Lista de Estudo 2", tasks)
        self.assertIn("Revisar Anotações", tasks)

    @patch("httpx.AsyncClient.patch")
    @patch("httpx.AsyncClient.delete")
    @patch("httpx.AsyncClient.get")
    async def test_update_daily_checklist_preserves_button(self, mock_get, mock_delete, mock_patch):
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = {
            "results": [
                {
                    "id": "btn-1234-unsupported",
                    "type": "unsupported"  # Representa o botão interativo [ e aí? ]
                },
                {
                    "id": "todo-old-1",
                    "type": "to_do"
                },
                {
                    "id": "todo-old-2",
                    "type": "to_do"
                }
            ]
        }
        mock_get.return_value = mock_get_resp

        mock_delete_resp = MagicMock()
        mock_delete_resp.status_code = 200
        mock_delete.return_value = mock_delete_resp

        mock_patch_resp = MagicMock()
        mock_patch_resp.status_code = 200
        mock_patch_resp.json.return_value = {"results": [{"id": "new-1"}]}
        mock_patch.return_value = mock_patch_resp

        res = await self.client.update_daily_checklist(
            custom_tasks=["Tarefa Nova 1", "Tarefa Nova 2"],
            notify_discord=False
        )

        self.assertTrue(res.get("success"))
        # Verifica que o DELETE foi chamado APENAS para os blocos to_do, NUNCA para o botão
        deleted_urls = [call.args[0] for call in mock_delete.call_args_list]
        self.assertEqual(len(deleted_urls), 2)
        self.assertTrue(any("todo-old-1" in u for u in deleted_urls))
        self.assertTrue(any("todo-old-2" in u for u in deleted_urls))
        self.assertFalse(any("btn-1234" in u for u in deleted_urls))

        # Verifica que o PATCH foi chamado com os novos blocos
        mock_patch.assert_called_once()
        patch_json = mock_patch.call_args.kwargs.get("json", {})
        children = patch_json.get("children", [])
        self.assertEqual(len(children), 2)
        self.assertEqual(children[0]["to_do"]["rich_text"][0]["text"]["content"], "Tarefa Nova 1")

    async def test_send_daily_checklist_announcement(self):
        notifier = MoodleDiscordNotifier(token="test_token", channel_id=123456)
        mock_channel = MagicMock()
        mock_channel.send = AsyncMock()

        with patch.object(notifier, "_resolve_channel", new_callable=AsyncMock) as mock_resolve:
            mock_resolve.return_value = mock_channel

            success = await notifier.send_daily_checklist_announcement(
                day_name="Quinta-feira",
                date_str="2026-09-10",
                checklist_items=["⭐ [Hoje] Estudo de Mandarim", "🎓 Química Geral B (14:00)"],
                notion_url="https://notion.so/test123"
            )

            self.assertTrue(success)
            mock_channel.send.assert_awaited_once()
            _, send_kwargs = mock_channel.send.call_args
            embed = send_kwargs.get("embed")
            self.assertIsNotNone(embed)
            self.assertIn("Quinta-feira", embed.title)
            self.assertIn("⭐ [Hoje] Estudo de Mandarim", embed.fields[0].value)


if __name__ == "__main__":
    unittest.main()
