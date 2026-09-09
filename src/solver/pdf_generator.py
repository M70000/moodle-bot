"""Gerador de relatórios e resoluções acadêmicas em PDF formatado no padrão UFMG.

Utiliza a API nativa do Chromium (Playwright) para transformar Markdown em um documento
A4 estilizado com cabeçalho institucional, tipografia elegante e suporte a código.
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from markdown_it import MarkdownIt
from playwright.async_api import async_playwright
from rich.console import Console

if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console()

STUDENT_CSS = """
@page {
    size: A4;
    margin: 20mm 20mm 20mm 20mm;
    @bottom-right {
        content: counter(page);
        font-family: Arial, sans-serif;
        font-size: 9pt;
        color: #888;
    }
}

body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    color: #1a1a1a;
    line-height: 1.5;
    font-size: 11pt;
    margin: 0;
    padding: 0;
}

.student-header {
    margin-bottom: 22px;
    padding-bottom: 10px;
    border-bottom: 1px solid #d0d7de;
}

.student-header .assignment-title {
    font-size: 16pt;
    font-weight: 700;
    color: #111111;
    margin: 0 0 4px 0;
}

.student-header .student-name {
    font-size: 11pt;
    font-weight: 500;
    color: #333333;
}

h1 {
    font-size: 15pt;
    color: #111111;
    margin-top: 18px;
    margin-bottom: 10px;
}

h2 {
    font-size: 13pt;
    color: #222222;
    margin-top: 16px;
    margin-bottom: 8px;
}

h3 {
    font-size: 11.5pt;
    color: #333333;
    margin-top: 14px;
    margin-bottom: 6px;
}

p {
    margin: 0 0 10px 0;
    text-align: left;
}

ul, ol {
    margin: 0 0 12px 0;
    padding-left: 24px;
}

li {
    margin-bottom: 4px;
}

table {
    width: 100%;
    border-collapse: collapse;
    margin: 14px 0;
    font-size: 10pt;
}

table th, table td {
    border: 1px solid #d0d7de;
    padding: 7px 10px;
    text-align: left;
}

table th {
    background-color: #f6f8fa;
    color: #111111;
    font-weight: 600;
}

table tr:nth-child(even) {
    background-color: #fafbfc;
}

pre {
    background-color: #f6f8fa;
    color: #24292f;
    padding: 12px;
    border-radius: 4px;
    border: 1px solid #d0d7de;
    overflow-x: auto;
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 9.5pt;
    line-height: 1.4;
    margin: 12px 0;
}

code {
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 10pt;
    background-color: #f6f8fa;
    color: #24292f;
    padding: 2px 4px;
    border-radius: 3px;
}

pre code {
    background-color: transparent;
    color: inherit;
    padding: 0;
    border: none;
}
"""


class AcademicPDFGenerator:
    """Converte relatórios e resoluções Markdown em PDFs acadêmicos de alta qualidade."""

    def __init__(self):
        self.md_parser = MarkdownIt("commonmark", {"html": True}).enable("table")

    def _build_html(
        self,
        markdown_text: str,
        course_name: str,
        assignment_title: str,
        student_name: str = "Moisés Mota Azevedo e Figueiredo",
        department: Optional[str] = None
    ) -> str:
        """Gera o HTML com cabeçalho limpo de estudante real."""
        rendered_body = self.md_parser.render(markdown_text)

        html_template = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <title>{assignment_title}</title>
    <style>
        {STUDENT_CSS}
    </style>
</head>
<body>
    <div class="student-header">
        <div class="assignment-title">{assignment_title}</div>
        <div class="student-name">{student_name}</div>
    </div>

    <div class="content-body">
        {rendered_body}
    </div>
</body>
</html>
"""
        return html_template
        return html_template

    async def render_pdf(
        self,
        markdown_text: str,
        output_pdf_path: Path,
        course_name: str,
        assignment_title: str,
        student_name: str = "Moisés Mota Azevedo e Figueiredo"
    ) -> Path:
        """Renderiza o Markdown em um arquivo PDF A4."""
        output_pdf_path.parent.mkdir(parents=True, exist_ok=True)
        html_content = self._build_html(
            markdown_text=markdown_text,
            course_name=course_name,
            assignment_title=assignment_title,
            student_name=student_name
        )

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html_content, wait_until="networkidle")

                await page.pdf(
                    path=str(output_pdf_path),
                    format="A4",
                    print_background=True,
                    margin={
                        "top": "20mm",
                        "bottom": "20mm",
                        "left": "20mm",
                        "right": "20mm"
                    }
                )
                console.print(f"[green]✔ PDF Acadêmico gerado com sucesso:[/green] {output_pdf_path.name}")
                return output_pdf_path
            finally:
                await browser.close()


async def main():
    """Teste rápido do gerador de PDF acadêmico."""
    gen = AcademicPDFGenerator()
    test_md = """# Relatório de Resolução: Exercício 1 - Estatística Descritiva

## 1. Resumo Executivo
Os dados de maratonistas contidos no arquivo `Maratona.csv` foram processados no ambiente **R**.
As estatísticas descritivas indicam que a distribuição de tempos apresenta assimetria positiva moderada, com média de 214.5 minutos e desvio padrão de 32.1 minutos.

## 2. Análise de Dados e Resolução Passo a Passo

### Questão 1: Medidas de Tendência Central e Dispersão
Com base nos conceitos de estatística descritiva e nas notas de aula da disciplina:

| Métrica | Valor Amostral | Unidade |
|---|---|---|
| Média | 214.50 | minutos |
| Mediana | 210.00 | minutos |
| Desvio Padrão | 32.14 | minutos |
| Amplitude Interquartil (IQR) | 41.20 | minutos |

### Questão 2: Código de Análise em R
Abaixo segue o script implementado em R:

```R
# Leitura da base de dados e cálculo descritivo
dados <- read.csv("Maratona.csv", header = TRUE)
summary(dados$tempo)

# Boxplot descritivo
boxplot(dados$tempo, col = "lightblue", main = "Distribuição dos Tempos de Maratona",
        ylab = "Tempo (minutos)")
```

## 3. Conclusão
O relatório cumpre todos os requisitos definidos no enunciado, com desenvolvimento analítico fundamentado nos slides da disciplina.
"""
    out = Path("storage/submissions/teste_academic_report.pdf")
    await gen.render_pdf(
        markdown_text=test_md,
        output_pdf_path=out,
        course_name="2026_2 - FUNDAMENTOS DE ESTATÍSTICA E CIÊNCIA DE DADOS - TB1",
        assignment_title="Exercício 1 - Estatística Descritiva"
    )
    console.print(f"Tamanho do arquivo: {out.stat().st_size} bytes")


if __name__ == "__main__":
    asyncio.run(main())
