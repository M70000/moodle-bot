import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from src.scraper.moodle_scraper import CourseAnnouncement
from src.scheduler.state import DaemonState

class TestAnnouncements(unittest.TestCase):
    def test_course_announcement_model(self):
        ann = CourseAnnouncement(
            id="12317",
            course_id="8206",
            course_name="Fundamentos de Estatística",
            title="Próxima semana",
            author="Glaura da Conceicao Franco",
            date="terça-feira, 8 set. 2026, 16:25",
            url="https://virtual.ufmg.br/20262/mod/forum/discuss.php?d=12317",
            message="Boa tarde,\nNesta quinta teremos aula normal..."
        )
        self.assertEqual(ann.id, "12317")
        self.assertEqual(ann.author, "Glaura da Conceicao Franco")
        self.assertIn("aula normal", ann.message)

    def test_daemon_state_announcements(self):
        with TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / "state.json"
            state = DaemonState(file_path=state_file)
            
            self.assertFalse(state.is_announcement_seen("12317"))
            self.assertEqual(len(state.get_known_announcement_ids()), 0)

            ann = CourseAnnouncement(
                id="12317",
                course_id="8206",
                course_name="Fundamentos de Estatística",
                title="Próxima semana",
                author="Glaura",
                date="8 set. 2026",
                url="https://virtual.ufmg.br/20262/mod/forum/discuss.php?d=12317",
                message="Teste"
            )
            state.mark_announcement_seen(ann)

            self.assertTrue(state.is_announcement_seen("12317"))
            self.assertIn("12317", state.get_known_announcement_ids())

            # Recarrega do arquivo
            reloaded_state = DaemonState(file_path=state_file)
            self.assertTrue(reloaded_state.is_announcement_seen("12317"))

if __name__ == "__main__":
    unittest.main()
