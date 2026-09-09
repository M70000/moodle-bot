import asyncio
import tempfile
import unittest
from pathlib import Path
from src.solver.pdf_generator import AcademicPDFGenerator, NumberedCanvas


class TestAcademicPDFGenerator(unittest.TestCase):
    def setUp(self):
        self.generator = AcademicPDFGenerator()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_format_inline_markdown(self):
        """Testa conversão de negrito, itálico, código e escape de caracteres XML."""
        raw_text = "Texto com **negrito**, *itálico*, `código_inline` e símbolos <tag> & valores."
        formatted = self.generator._format_inline_markdown(raw_text)

        self.assertIn("<b>negrito</b>", formatted)
        self.assertIn("<i>itálico</i>", formatted)
        self.assertIn('<font face="Courier"', formatted)
        self.assertIn("código_inline", formatted)
        # Verifica que símbolos foram escapados para evitar erro de XML no ReportLab
        self.assertIn("&lt;tag&gt;", formatted)
        self.assertIn("&amp;", formatted)

    def test_render_pdf_sync_single_page(self):
        """Testa geração síncrona de PDF com tabelas, código e formatação básica."""
        sample_md = """# Resolução de Exercício Prático
        
## 1. Introdução
Este é um teste de parágrafo com **destaque** e *ênfase*.

> Esta é uma nota explicativa relevante sobre o método empregado.

| Parâmetro | Valor | Unidade |
|---|---|---|
| Taxa de Aprendizado | 0.01 | adimensional |
| Épocas | 50 | iterações |

- Item 1 de lista
- Item 2 com `código`

```python
def calcular_media(valores):
    return sum(valores) / len(valores)
```
"""
        out_pdf = self.temp_path / "teste_sync.pdf"
        result_path = self.generator.render_pdf_sync(
            markdown_text=sample_md,
            output_pdf_path=out_pdf,
            course_name="Fundamentos de Estatística",
            assignment_title="Exercício 1"
        )

        self.assertTrue(result_path.exists())
        self.assertGreater(result_path.stat().st_size, 1000)

        # Valida header padrão de PDF
        with open(result_path, "rb") as f:
            header = f.read(5)
            self.assertEqual(header, b"%PDF-")

    def test_render_pdf_async(self):
        """Testa método assíncrono render_pdf."""
        sample_md = """# Relatório Assíncrono
        
Texto de teste executado de forma não-bloqueante no event loop.
"""
        out_pdf = self.temp_path / "teste_async.pdf"
        result_path = asyncio.run(
            self.generator.render_pdf(
                markdown_text=sample_md,
                output_pdf_path=out_pdf,
                course_name="Programação Concorrente",
                assignment_title="Trabalho Prático 2"
            )
        )

        self.assertTrue(result_path.exists())
        self.assertGreater(result_path.stat().st_size, 1000)

    def test_render_multi_page_pdf(self):
        """Testa geração de documento com múltiplas páginas para verificar NumberedCanvas e cabeçalhos."""
        paragraphs = []
        for i in range(1, 25):
            paragraphs.append(f"## Seção {i}\n\nEste é um parágrafo longo repetido para forçar a quebra de página automática no ReportLab. " * 5)

        multi_page_md = "\n\n".join(paragraphs)
        out_pdf = self.temp_path / "teste_multipage.pdf"

        result_path = self.generator.render_pdf_sync(
            markdown_text=multi_page_md,
            output_pdf_path=out_pdf,
            course_name="Engenharia de Software",
            assignment_title="Relatório Completo Multi-página"
        )

        self.assertTrue(result_path.exists())
        # Deve ter gerado um arquivo maior com múltiplas páginas
        self.assertGreater(result_path.stat().st_size, 3000)

        with open(result_path, "rb") as f:
            content = f.read()
            self.assertTrue(content.startswith(b"%PDF-"))


if __name__ == "__main__":
    unittest.main()
