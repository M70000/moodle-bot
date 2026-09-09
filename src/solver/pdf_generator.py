"""Gerador de relatórios e resoluções acadêmicas em PDF limpo e natural.

Utiliza a biblioteca ReportLab para produzir PDFs formatados com aspecto natural de
trabalho feito por aluno (estilo Microsoft Word / Google Docs exportado), sem
cabeçalhos ou rodapés institucionais artificiais que pareçam gerados por IA ou templates prontos.
"""

import asyncio
import html
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
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


class StudentDocumentCanvas(canvas.Canvas):
    """Canvas com numeração de páginas discreta (estilo Word / Google Docs).

    Nenhum cabeçalho ou rodapé institucional é inserido para manter a aparência genuína
    de um documento elaborado pelo próprio discente.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []
        self.assignment_title = ""
        self.course_name = ""

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_decorations(self, page_count: int):
        # Apenas numeração discreta se houver mais de uma página (ex: "2 / 3")
        # Sem faixas institucionais, sem UFMG repetido, sem bordas pesadas
        if page_count > 1:
            self.saveState()
            page_w, _ = A4
            right_m = page_w - 54.0
            self.setFont("Helvetica", 8.5)
            self.setFillColor(colors.HexColor("#94A3B8"))
            self.drawRightString(right_m, 32, f"{self._pageNumber} / {page_count}")
            self.restoreState()


# Alias para retrocompatibilidade
NumberedCanvas = StudentDocumentCanvas


class AcademicPDFGenerator:
    """Converte relatórios e resoluções Markdown em PDFs com formatação natural de aluno."""

    def __init__(self):
        self.styles = getSampleStyleSheet()
        self._init_custom_styles()

    def _init_custom_styles(self):
        """Inicializa estilos tipográficos limpos, no padrão de documentos acadêmicos comuns."""
        self.title_style = ParagraphStyle(
            "DocTitle",
            parent=self.styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=15,
            leading=19,
            textColor=colors.HexColor("#111827"),
            spaceAfter=4,
        )
        self.meta_style = ParagraphStyle(
            "DocMeta",
            parent=self.styles["Normal"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=13,
            textColor=colors.HexColor("#4B5563"),
            spaceAfter=6,
        )
        self.h1_style = ParagraphStyle(
            "CustomH1",
            parent=self.styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=12.5,
            leading=16,
            textColor=colors.HexColor("#111827"),
            spaceBefore=13,
            spaceAfter=5,
            keepWithNext=True,
        )
        self.h2_style = ParagraphStyle(
            "CustomH2",
            parent=self.styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=14.5,
            textColor=colors.HexColor("#1F2937"),
            spaceBefore=10,
            spaceAfter=4,
            keepWithNext=True,
        )
        self.h3_style = ParagraphStyle(
            "CustomH3",
            parent=self.styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=13,
            textColor=colors.HexColor("#374151"),
            spaceBefore=8,
            spaceAfter=3,
            keepWithNext=True,
        )
        self.body_style = ParagraphStyle(
            "CustomBody",
            parent=self.styles["Normal"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=14,
            textColor=colors.HexColor("#1F2937"),
            spaceAfter=6,
        )
        self.bullet_style = ParagraphStyle(
            "CustomBullet",
            parent=self.body_style,
            leftIndent=16,
            firstLineIndent=-10,
            spaceAfter=3,
        )
        self.table_cell_style = ParagraphStyle(
            "TableCell",
            parent=self.styles["Normal"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=11.5,
            textColor=colors.HexColor("#1F2937"),
        )
        self.table_header_style = ParagraphStyle(
            "TableHeader",
            parent=self.styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8.5,
            leading=11.5,
            textColor=colors.HexColor("#111827"),
        )
        self.code_style = ParagraphStyle(
            "CodeBlock",
            parent=self.styles["Normal"],
            fontName="Courier",
            fontSize=8.5,
            leading=11.5,
            textColor=colors.HexColor("#111827"),
        )

    def _format_inline_markdown(self, text: str) -> str:
        """Converte marcações inline (bold, italic, code) em tags XML suportadas pelo ReportLab."""
        clean = html.escape(text.strip())

        # Código inline `code`
        clean = re.sub(
            r"`([^`]+)`",
            r'<font face="Courier" color="#111827" backcolor="#F3F4F6">&nbsp;<b>\1</b>&nbsp;</font>',
            clean,
        )

        # Negrito **bold** ou __bold__
        clean = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", clean)
        clean = re.sub(r"__([^_]+)__", r"<b>\1</b>", clean)

        # Itálico *italic* ou _italic_
        clean = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", clean)
        clean = re.sub(r"(?<!_)_([^_]+)_(?!_)", r"<i>\1</i>", clean)

        return clean

    def _parse_markdown_to_flowables(self, markdown_text: str, printable_width: float) -> List:
        """Converte blocos de Markdown estruturado em Flowables Platypus do ReportLab."""
        flowables = []
        lines = markdown_text.splitlines()
        idx = 0
        total_lines = len(lines)

        while idx < total_lines:
            line = lines[idx]
            stripped = line.strip()

            if not stripped:
                idx += 1
                continue

            # 1. Bloco de Código Fenced ```
            if stripped.startswith("```"):
                code_lines = []
                idx += 1
                while idx < total_lines and not lines[idx].strip().startswith("```"):
                    code_lines.append(html.escape(lines[idx]))
                    idx += 1
                if idx < total_lines:
                    idx += 1  # consome o ``` de fechamento

                code_content = "<br/>".join(code_lines) if code_lines else "&nbsp;"
                p_code = Paragraph(code_content, self.code_style)

                code_table = Table([[p_code]], colWidths=[printable_width])
                code_table.setStyle(
                    TableStyle([
                        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F9FAFB")),
                        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#E5E7EB")),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ])
                )
                flowables.append(Spacer(1, 3))
                flowables.append(code_table)
                flowables.append(Spacer(1, 5))
                continue

            # 2. Tabela Markdown | Col 1 | Col 2 |
            if stripped.startswith("|") and stripped.endswith("|"):
                table_lines = []
                while (
                    idx < total_lines
                    and lines[idx].strip().startswith("|")
                    and lines[idx].strip().endswith("|")
                ):
                    t_line = lines[idx].strip()
                    # Ignora linhas delimitadoras como |---|---|
                    if not re.match(r"^\|[\s\-:]+(\|[\s\-:]+)+\|$", t_line):
                        cells = [c.strip() for c in t_line.strip("|").split("|")]
                        table_lines.append(cells)
                    idx += 1

                if table_lines:
                    num_cols = max(len(row) for row in table_lines)
                    normalized_rows = []
                    for r_idx, row in enumerate(table_lines):
                        padded = row + [""] * (num_cols - len(row))
                        is_header = (r_idx == 0)
                        row_style = self.table_header_style if is_header else self.table_cell_style
                        row_paragraphs = [
                            Paragraph(self._format_inline_markdown(cell), row_style)
                            for cell in padded
                        ]
                        normalized_rows.append(row_paragraphs)

                    col_w = printable_width / num_cols
                    table_flowable = Table(normalized_rows, colWidths=[col_w] * num_cols)
                    table_flowable.setStyle(
                        TableStyle([
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
                            ("LINEBELOW", (0, 0), (-1, 0), 1.0, colors.HexColor("#9CA3AF")),
                            (
                                "ROWBACKGROUNDS",
                                (0, 1),
                                (-1, -1),
                                [colors.white, colors.HexColor("#F9FAFB")],
                            ),
                            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
                            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#D1D5DB")),
                            ("TOPPADDING", (0, 0), (-1, -1), 5),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                            ("LEFTPADDING", (0, 0), (-1, -1), 6),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                        ])
                    )
                    flowables.append(Spacer(1, 3))
                    flowables.append(table_flowable)
                    flowables.append(Spacer(1, 5))
                continue

            # 3. Cabeçalhos #, ##, ###, ####
            if stripped.startswith("#### "):
                title_text = self._format_inline_markdown(stripped[5:])
                flowables.append(Paragraph(title_text, self.h3_style))
                idx += 1
                continue
            elif stripped.startswith("### "):
                title_text = self._format_inline_markdown(stripped[4:])
                flowables.append(Paragraph(title_text, self.h3_style))
                idx += 1
                continue
            elif stripped.startswith("## "):
                title_text = self._format_inline_markdown(stripped[3:])
                flowables.append(Paragraph(title_text, self.h2_style))
                idx += 1
                continue
            elif stripped.startswith("# "):
                title_text = self._format_inline_markdown(stripped[2:])
                flowables.append(Paragraph(title_text, self.h1_style))
                idx += 1
                continue

            # 4. Linhas divisórias --- ou ***
            if re.match(r"^(\*{3,}|-{3,}|_{3,})$", stripped):
                flowables.append(Spacer(1, 4))
                flowables.append(
                    HRFlowable(
                        width="100%",
                        thickness=0.6,
                        color=colors.HexColor("#E5E7EB"),
                        spaceAfter=8,
                        spaceBefore=4,
                    )
                )
                idx += 1
                continue

            # 5. Itens de Lista (- item, * item, 1. item)
            list_match = re.match(r"^(\d+\.|\*|-)\s+(.+)", stripped)
            if list_match:
                bullet_prefix = list_match.group(1)
                item_text = list_match.group(2)
                bullet_display = (
                    "&bull; " if bullet_prefix in ["-", "*"] else f"<b>{bullet_prefix}</b> "
                )
                formatted_item = self._format_inline_markdown(item_text)
                flowables.append(
                    Paragraph(f"{bullet_display}{formatted_item}", self.bullet_style)
                )
                idx += 1
                continue

            # 6. Citação / Nota (> texto)
            if stripped.startswith(">"):
                quote_lines = []
                while idx < total_lines and lines[idx].strip().startswith(">"):
                    quote_lines.append(lines[idx].strip().lstrip(">").strip())
                    idx += 1
                quote_text = " ".join(quote_lines)
                p_quote = Paragraph(
                    f"<i>{self._format_inline_markdown(quote_text)}</i>", self.body_style
                )
                quote_table = Table([[p_quote]], colWidths=[printable_width])
                quote_table.setStyle(
                    TableStyle([
                        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F9FAFB")),
                        ("LINEBEFORE", (0, 0), (0, -1), 2.5, colors.HexColor("#6B7280")),
                        ("TOPPADDING", (0, 0), (-1, -1), 5),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ])
                )
                flowables.append(Spacer(1, 3))
                flowables.append(quote_table)
                flowables.append(Spacer(1, 5))
                continue

            # 7. Parágrafo de texto comum
            para_lines = []
            while (
                idx < total_lines
                and lines[idx].strip()
                and not lines[idx].strip().startswith(("#", "```", "|", ">", "---"))
            ):
                cur_stripped = lines[idx].strip()
                # Interrompe se encontrar um item de lista (- item, * item, 1. item)
                if re.match(r"^(\d+\.|\*|-)\s+", cur_stripped):
                    break
                # Interrompe se for linha divisória (***, ---, ___)
                if re.match(r"^(\*{3,}|-{3,}|_{3,})$", cur_stripped):
                    break
                para_lines.append(cur_stripped)
                idx += 1

            if para_lines:
                full_para = " ".join(para_lines)
                flowables.append(
                    Paragraph(self._format_inline_markdown(full_para), self.body_style)
                )
            else:
                # Salvaguarda absoluta: se nenhuma regra consumiu a linha, avança idx para evitar loop infinito
                idx += 1

        return flowables

    def render_pdf_sync(
        self,
        markdown_text: str,
        output_pdf_path: Path,
        course_name: str,
        assignment_title: str,
        student_name: str = "Moisés Mota Azevedo e Figueiredo",
    ) -> Path:
        """Gera o documento PDF de forma síncrona com ReportLab."""
        output_pdf_path.parent.mkdir(parents=True, exist_ok=True)

        left_m = 54.0
        right_m = 54.0
        top_m = 54.0
        bottom_m = 54.0
        printable_width = A4[0] - left_m - right_m

        doc = SimpleDocTemplate(
            str(output_pdf_path),
            pagesize=A4,
            leftMargin=left_m,
            rightMargin=right_m,
            topMargin=top_m,
            bottomMargin=bottom_m,
        )

        story = []

        # Limpeza de prefixos brutos de sistema (ex: "2026_2 - " ou " - TB1")
        clean_course = course_name
        clean_course = re.sub(r"^\d{4}_\d\s*-\s*", "", clean_course)
        clean_course = re.sub(
            r"\s*-\s*(T[A-Z0-9]+|METATURMA)$", "", clean_course, flags=re.IGNORECASE
        ).strip()

        # Cabeçalho limpo de aluno real (Título do trabalho + identificação)
        story.append(Paragraph(self._format_inline_markdown(assignment_title), self.title_style))

        meta_parts = [f"<b>Aluno:</b> {html.escape(student_name)}"]
        if clean_course:
            meta_parts.append(f"<b>Disciplina:</b> {html.escape(clean_course.title())}")

        meta_line = " &nbsp;&nbsp;|&nbsp;&nbsp; ".join(meta_parts)
        story.append(Paragraph(meta_line, self.meta_style))
        story.append(
            HRFlowable(
                width="100%",
                thickness=0.6,
                color=colors.HexColor("#E2E8F0"),
                spaceAfter=14,
                spaceBefore=2,
            )
        )

        # Converte Markdown para Flowables
        body_flowables = self._parse_markdown_to_flowables(markdown_text, printable_width)
        story.extend(body_flowables)

        # Constrói o PDF com Canvas de numeração simples
        def canvas_builder(*args, **kwargs):
            c = StudentDocumentCanvas(*args, **kwargs)
            c.assignment_title = assignment_title
            c.course_name = course_name
            return c

        doc.build(story, canvasmaker=canvas_builder)
        return output_pdf_path

    async def render_pdf(
        self,
        markdown_text: str,
        output_pdf_path: Path,
        course_name: str,
        assignment_title: str,
        student_name: str = "Moisés Mota Azevedo e Figueiredo",
    ) -> Path:
        """Renderiza o Markdown em um arquivo PDF A4 assincronamente."""
        pdf_path = await asyncio.to_thread(
            self.render_pdf_sync,
            markdown_text=markdown_text,
            output_pdf_path=output_pdf_path,
            course_name=course_name,
            assignment_title=assignment_title,
            student_name=student_name,
        )
        console.print(f"[green]✔ PDF gerado com sucesso:[/green] {output_pdf_path.name}")
        return pdf_path


# Alias para retrocompatibilidade
ReportLabPDFGenerator = AcademicPDFGenerator


async def main():
    """Teste rápido de geração de PDF no padrão de estudante."""
    gen = AcademicPDFGenerator()
    test_md = """# Resolução: Exercício 1 - Estatística Descritiva

## 1. Resumo
Os dados de maratonistas contidos no arquivo `Maratona.csv` foram processados no ambiente **R**.
As estatísticas descritivas indicam que a distribuição de tempos apresenta assimetria positiva moderada, com média de 214.5 minutos e desvio padrão de 32.1 minutos.

## 2. Análise de Dados e Resolução Passo a Passo

### Questão 1: Medidas de Tendência Central e Dispersão

| Métrica | Valor Amostral | Unidade |
|---|---|---|
| Média | 214.50 | minutos |
| Mediana | 210.00 | minutos |
| Desvio Padrão | 32.14 | minutos |
| Amplitude Interquartil (IQR) | 41.20 | minutos |

### Questão 2: Respostas
- **Resposta 1:** storm
- **Resposta 2:** winter
- **Resposta 3:** cold

1. Leitura e limpeza da base de dados
2. Cálculo das estatísticas univariadas
3. Construção do gráfico de dispersão

### Questão 3: Script em R

```R
dados <- read.csv("Maratona.csv", header = TRUE)
summary(dados$tempo)

boxplot(dados$tempo, col = "lightblue", main = "Distribuição dos Tempos de Maratona",
        ylab = "Tempo (minutos)")
```

## 3. Conclusão
O exercício foi concluído conforme solicitado no enunciado, conferindo os valores amostrais obtidos.
"""
    out = Path("storage/submissions/teste_aluno_natural.pdf")
    await gen.render_pdf(
        markdown_text=test_md,
        output_pdf_path=out,
        course_name="2026_2 - FUNDAMENTOS DE ESTATÍSTICA E CIÊNCIA DE DADOS - TB1",
        assignment_title="Exercício 1 - Estatística Descritiva",
    )
    console.print(f"Tamanho do arquivo gerado: {out.stat().st_size} bytes")


if __name__ == "__main__":
    asyncio.run(main())
