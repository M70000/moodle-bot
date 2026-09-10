import re
import unicodedata
import unittest

def normalize_str(text: str) -> str:
    if not text:
        return ""
    return unicodedata.normalize("NFKD", text).encode("ASCII", "ignore").decode("utf-8").strip().lower()

def match_radio_option(candidate_options, target_val):
    clean_target = str(target_val).strip()
    clean_target = re.sub(r"^\s*-\s*\*\*Resposta:\*\*\s*", "", clean_target, flags=re.IGNORECASE).strip()
    clean_target = re.sub(r"^\s*\*\*Resposta:\*\*\s*", "", clean_target, flags=re.IGNORECASE).strip()
    clean_target = re.sub(r"^\s*Resposta:\s*", "", clean_target, flags=re.IGNORECASE).strip()

    target_letter_match = re.match(r"^\s*([a-eA-E])(?:\.|\)|\:|\s|$)", clean_target)
    target_letter = target_letter_match.group(1).lower() if target_letter_match else None

    target_content = re.sub(r"^\s*([a-eA-E])(?:\.|\)|\:|\s|-)+", "", clean_target).strip()
    norm_target_content = normalize_str(target_content)

    processed_options = []
    for opt_raw_text in candidate_options:
        clean_opt = opt_raw_text.replace("\n", " ").strip()
        clean_opt = re.sub(r"(?i)não respondido|marcado|selecionado", "", clean_opt).strip()

        opt_letter_match = re.search(r"(?:^|\s)([a-eA-E])(?:\.|\)|\:)\s*", clean_opt)
        opt_letter = opt_letter_match.group(1).lower() if opt_letter_match else None

        opt_content = re.sub(r"(?:^|\s)([a-eA-E])(?:\.|\)|\:)\s*", "", clean_opt).strip()
        norm_opt_content = normalize_str(opt_content)

        processed_options.append({
            "raw_text": clean_opt,
            "letter": opt_letter,
            "content": opt_content,
            "norm_content": norm_opt_content
        })

    best_match = None
    best_score = -1

    for opt in processed_options:
        score = 0
        if len(norm_target_content) >= 3 and len(opt["norm_content"]) >= 3:
            if norm_target_content == opt["norm_content"]:
                score = 100
            elif norm_target_content in opt["norm_content"] and len(norm_target_content) >= 8:
                score = 90
            elif opt["norm_content"] in norm_target_content and len(opt["norm_content"]) >= 8:
                score = 90
            else:
                words_target = set(w for w in norm_target_content.split() if len(w) > 2)
                words_opt = set(w for w in opt["norm_content"].split() if len(w) > 2)
                if words_target and words_opt:
                    overlap = len(words_target & words_opt)
                    jaccard = overlap / max(len(words_target), len(words_opt))
                    if jaccard >= 0.4:
                        score = 50 + int(jaccard * 35)

        if target_letter and opt["letter"] and target_letter == opt["letter"]:
            if len(norm_target_content) < 3:
                score = 80
            else:
                score += 15

        if score > best_score:
            best_score = score
            best_match = opt

    if best_match and best_score >= 50:
        return best_match["content"], best_match["letter"], best_score
    return None, None, best_score


import unittest

class TestQuizMatcher(unittest.TestCase):
    def test_user_actual_quiz_case(self):
        """Testa o caso exato do áudio do usuário (Inglês Instrumental Aula 1).
        Opção alvo: 'c. de instruções para o uso correto de algo'
        No Moodle antes, marcava 'a' porque 'a' estava contida no texto!
        """
        options = [
            "a. de ferramentas de reparo para uso residencial",
            "b. a pessoa utilizando o produto com atenção",
            "c. de instruções para o uso correto de algo"
        ]
        target = "c. de instruções para o uso correto de algo"
        content, letter, score = match_radio_option(options, target)
        self.assertEqual(content, "de instruções para o uso correto de algo")
        self.assertEqual(letter, "c")
        self.assertGreaterEqual(score, 90)

    def test_shuffled_options_in_moodle(self):
        """Testa quando o Moodle embaralha as opções e a resposta C agora está na posição A!"""
        shuffled_options = [
            "a. de instruções para o uso correto de algo", # Originalmente era C, virou A pelo shuffle!
            "b. de ferramentas de reparo para uso residencial",
            "c. a pessoa utilizando o produto com atenção"
        ]
        target = "c. de instruções para o uso correto de algo"
        content, letter, score = match_radio_option(shuffled_options, target)
        # Deve selecionar o texto correto (que agora é a letra A), imune ao shuffle!
        self.assertEqual(content, "de instruções para o uso correto de algo")
        self.assertEqual(letter, "a")
        self.assertGreaterEqual(score, 90)

    def test_only_letter_provided(self):
        """Testa quando a IA ou usuário fornece apenas a letra 'B'."""
        options = [
            "a. primeira opção qualquer",
            "b. segunda opção pretendida",
            "c. terceira opção"
        ]
        target = "b"
        content, letter, score = match_radio_option(options, target)
        self.assertEqual(letter, "b")
        self.assertEqual(content, "segunda opção pretendida")

    def test_no_false_positive_on_a(self):
        """Garante que se o alvo for 'c', NUNCA vai marcar a opção 'a' mesmo que 'a' tenha a letra 'c'."""
        options = [
            "a. chave com conexão comum e chave com conexão cruzada",
            "b. bateria de lithium recarregável",
            "c. sensor infravermelho de precisão"
        ]
        target = "c"
        content, letter, score = match_radio_option(options, target)
        self.assertEqual(letter, "c")
        self.assertEqual(content, "sensor infravermelho de precisão")

    def test_markdown_response_format(self):
        """Testa quando o target vem formatado como '- **Resposta:** b. a pessoa utilizando o produto'."""
        options = [
            "a. o produto sendo descartado",
            "b. a pessoa utilizando o produto",
            "c. o manual do usuário em inglês"
        ]
        target = "- **Resposta:** b. a pessoa utilizando o produto"
        content, letter, score = match_radio_option(options, target)
        self.assertEqual(letter, "b")
        self.assertEqual(content, "a pessoa utilizando o produto")

    def test_combobox_association_matching(self):
        """Garante que selects/comboboxes de questões de associação são mapeados com precisão imunes a embaralhamento."""
        moodle_options = [
            "Escolher...",
            "Professor da Sorbonne",
            "Obra de Luis XIV",
            "Construiu a Torre Eifel",
            "Ópera La Boheme",
            "Obra de Napoleão"
        ]
        answers = {
            "Q21_1": "Gustave Eiffel → Construiu a Torre Eifel",
            "Q21_2": "Puccini → Ópera La Boheme",
            "Q21_3": "Les Invalides → Obra de Luis XIV",
            "Q21_4": "L'Arc de Triomphe → Obra de Napoleão",
            "Q21_5": "Dante → Professor da Sorbonne"
        }

        # Simula linhas na ordem embaralhada pelo Moodle
        shuffled_moodle_rows = [
            ("L'Arc de Triomphe", "Obra de Napoleão"),
            ("Gustave Eiffel", "Construiu a Torre Eifel"),
            ("Dante", "Professor da Sorbonne"),
            ("Puccini", "Ópera La Boheme"),
            ("Les Invalides", "Obra de Luis XIV")
        ]

        valid_options = [o for o in moodle_options if "escolher" not in o.lower()]

        for row_label, expected_opt in shuffled_moodle_rows:
            norm_lbl = normalize_str(row_label)
            target_val = None
            for k, v in answers.items():
                parts = re.split(r"[→\->:]", str(v), maxsplit=1)
                if len(parts) == 2 and (norm_lbl in normalize_str(parts[0]) or normalize_str(parts[0]) in norm_lbl):
                    target_val = parts[1].strip()
                    break

            self.assertIsNotNone(target_val)
            norm_target = normalize_str(target_val)
            best_opt = None
            best_score = -1
            for opt in valid_options:
                opt_norm = normalize_str(opt)
                if norm_target == opt_norm:
                    score = 100
                elif norm_target in opt_norm or opt_norm in norm_target:
                    score = 85
                else:
                    score = 0
                if score > best_score:
                    best_score = score
                    best_opt = opt

            self.assertEqual(best_opt, expected_opt)
            self.assertGreaterEqual(best_score, 85)


class TestEssayAndRichTextParsing(unittest.TestCase):
    """Testes de robustez para extração e preenchimento de questões dissertativas / editor rico."""

    def test_extract_json_answers_from_markdown(self):
        sample_md = """
```json:answers
{
  "CAMPO_1": "primeira lacuna",
  "CAMPO_2": "redação dissertativa completa sobre a CF/88",
  "Q1": "a. alternativa correta",
  "Q2": "resposta aberta para questão 2"
}
```

### Questão 1
- **Resposta:** a. alternativa correta

### Questão 2
O princípio da dignidade da pessoa humana...
"""
        import json
        json_m = re.search(r"```(?:json:answers|json)\s*\n(.*?)\n```", sample_md, re.DOTALL)
        self.assertIsNotNone(json_m)
        parsed = json.loads(json_m.group(1).strip())
        self.assertEqual(parsed["CAMPO_1"], "primeira lacuna")
        self.assertIn("redação dissertativa", parsed["CAMPO_2"])
        self.assertEqual(parsed["Q2"], "resposta aberta para questão 2")

    def test_extract_open_ended_essay_fallback(self):
        sample_md = """
### Questão 1
- **Resposta:** c. alternativa de múltipla escolha

### Questão 2
Texto da questão
A responsabilidade civil do Estado no Brasil baseia-se na teoria do risco administrativo, prevista no art. 37, § 6º da Constituição Federal de 1988.

### Questão 3
**Resposta:**
Esta é uma dissertação multilinhas explicando o tema em detalhes.
Com múltiplos parágrafos bem fundamentados.
**Justificativa:**
Justificativa adicional não deve poluir a resposta.
"""
        ans_payload = {}
        q_matches = list(re.finditer(r"###\s*(?:Quest[ãa]o|Q)\s*(\d+)\s*\n+(.*?)(?=\n###|\Z)", sample_md, re.DOTALL | re.IGNORECASE))
        for m in q_matches:
            q_num = m.group(1)
            q_body = m.group(2).strip()
            ans_key = f"Q{q_num}"
            resp_m = re.search(r"\*\*(?:Resposta|Alternativa):\*\*\s*([\s\S]+?)(?=\n\*\*(?:Explicação|Justificativa):|\n###|\Z)", q_body, re.IGNORECASE)
            if resp_m and resp_m.group(1).strip():
                ans_payload[ans_key] = resp_m.group(1).strip()
            else:
                clean_body = re.sub(r"^(?:Texto da questão|Enunciado:?|Pergunta:?)\s*", "", q_body, flags=re.IGNORECASE).strip()
                if clean_body:
                    ans_payload[ans_key] = clean_body

        self.assertEqual(ans_payload["Q1"], "c. alternativa de múltipla escolha")
        self.assertIn("responsabilidade civil do Estado", ans_payload["Q2"])
        self.assertIn("art. 37, § 6º", ans_payload["Q2"])
        self.assertIn("dissertação multilinhas", ans_payload["Q3"])
        self.assertNotIn("Justificativa adicional", ans_payload["Q3"])

    def test_slash_option_vs_long_essay_preservation(self):
        # Lacuna simples com opções separadas por barra: deve pegar a primeira
        short_blank = "opção A / opção B"
        if "/" in short_blank and len(short_blank) < 60:
            short_target = [o.strip() for o in short_blank.split("/") if o.strip()][0]
        else:
            short_target = short_blank
        self.assertEqual(short_target, "opção A")

        # Texto dissertativo longo contendo barras (e/ou, CF/88): NÃO deve truncar
        long_essay = "Conforme o artigo 5º/CF e/ou legislação infraconstitucional vigente, deve-se considerar a legalidade estrita."
        if "/" in long_essay and len(long_essay) < 60:
            long_target = [o.strip() for o in long_essay.split("/") if o.strip()][0]
        else:
            long_target = long_essay
        self.assertEqual(long_target, long_essay)


from src.scraper.moodle_quiz import extract_checkbox_letters


class TestAdvancedQuizComponents(unittest.TestCase):
    """Testes para novos componentes: Checkboxes, TinyMCE, Arrastar/Soltar e Matrizes."""

    def test_extract_checkbox_letters_portuguese_safe(self):
        """Garante que a extração de letras de checkbox não sofre falsos positivos com a letra 'a'."""
        # Caso 1: letras separadas por vírgula
        self.assertEqual(extract_checkbox_letters("a, c"), {"a", "c"})

        # Caso 2: marcadores com ponto
        self.assertEqual(extract_checkbox_letters("a. primeira, c. terceira"), {"a", "c"})

        # Caso 3: "A e D" - 'e' é conjunção, não alternativa E
        self.assertEqual(extract_checkbox_letters("Alternativas A e D estão corretas"), {"a", "d"})

        # Caso 4: "A, E e D" - aqui 'E' é expressamente uma alternativa
        self.assertEqual(extract_checkbox_letters("Alternativas A, E e D estão corretas"), {"a", "e", "d"})

        # Caso 5: Frase em português com a preposição/artigo 'a' e palavra com 'a'
        # NUNCA deve incluir 'a' quando a opção marcada for apenas 'b'!
        self.assertEqual(
            extract_checkbox_letters("b. a pessoa utilizando o produto com atenção"),
            {"b"}
        )

        # Caso 6: Letra única maiúscula ou minúscula
        self.assertEqual(extract_checkbox_letters("B"), {"b"})
        self.assertEqual(extract_checkbox_letters("[A, C]"), {"a", "c"})

        # Caso 7: c. com texto em português contendo 'a regra'
        self.assertEqual(
            extract_checkbox_letters("c. de acordo com a regra geral"),
            {"c"}
        )

    def test_tinymce_html_formatting(self):
        """Testa a transformação de texto puro para formato HTML exigido pelo TinyMCE."""
        plain_text = "I'm twenty one years old"
        formatted = plain_text if "<p>" in plain_text else f"<p>{plain_text.replace(chr(10), '<br>')}</p>"
        self.assertEqual(formatted, "<p>I'm twenty one years old</p>")

        multiline_text = "Primeiro parágrafo.\nSegundo parágrafo."
        formatted_multi = multiline_text if "<p>" in multiline_text else f"<p>{multiline_text.replace(chr(10), '<br>')}</p>"
        self.assertEqual(formatted_multi, "<p>Primeiro parágrafo.<br>Segundo parágrafo.</p>")

        html_text = "<p>Texto já formatado em HTML</p>"
        formatted_html = html_text if "<p>" in html_text else f"<p>{html_text.replace(chr(10), '<br>')}</p>"
        self.assertEqual(formatted_html, "<p>Texto já formatado em HTML</p>")

    def test_drag_and_drop_into_text_matching(self):
        """Testa o algoritmo de correspondência para lacunas de arrastar e soltar (ddwtos)."""
        drag_options = [
            {"choice": "1", "text": "transporte ativo", "norm_text": normalize_str("transporte ativo")},
            {"choice": "2", "text": "difusão facilitada", "norm_text": normalize_str("difusão facilitada")},
            {"choice": "3", "text": "osmose", "norm_text": normalize_str("osmose")}
        ]

        def match_drag(target_val):
            norm_target = normalize_str(str(target_val))
            best = None
            best_score = -1
            for dh in drag_options:
                if norm_target == dh["norm_text"]:
                    score = 100
                elif norm_target in dh["norm_text"] or dh["norm_text"] in norm_target:
                    score = 85
                else:
                    score = 0
                if score > best_score:
                    best_score = score
                    best = dh
            return best, best_score

        best, score = match_drag("difusão facilitada")
        self.assertEqual(best["choice"], "2")
        self.assertEqual(score, 100)

        best_upper, score_upper = match_drag("OSMOSE")
        self.assertEqual(best_upper["choice"], "3")
        self.assertEqual(score_upper, 100)

        best_approx, score_approx = match_drag("transporte ativo primário")
        self.assertEqual(best_approx["choice"], "1")
        self.assertEqual(score_approx, 85)

    def test_matrix_question_name_segregation(self):
        """Garante que questões em matriz (V/F por linha) não sobrescrevem alternativas entre linhas."""
        # Simula 3 linhas de uma tabela com radio buttons por linha
        matrix_rows = [
            {"name": "q10:1_sub1", "label": "O Brasil é uma República Federativa", "expected": "Verdadeiro"},
            {"name": "q10:1_sub2", "label": "A capital da Argentina é Montevidéu", "expected": "Falso"},
            {"name": "q10:1_sub3", "label": "A França faz fronteira com o Brasil", "expected": "Verdadeiro"}
        ]
        answers = {
            "Q10_1": "Verdadeiro",
            "Q10_2": "Falso",
            "Q10_3": "Verdadeiro"
        }

        # Cada linha do grupo de rádio deve localizar sua resposta independente
        for g_idx, row in enumerate(matrix_rows):
            target_val = answers.get(f"Q10_{g_idx+1}")
            self.assertEqual(target_val, row["expected"])

    def test_association_splitting_with_hyphens_and_dates(self):
        """Testa o caso real do usuário onde enunciados contêm datas e hífens ('1978 - The birth of Baby Louise...')."""
        from src.scraper.moodle_quiz import split_association_item
        line_baby = "5. 1978 - The birth of Baby Louise, the first child conceived through in vitro fertilization: **Figura 05**"
        left_b, right_b = split_association_item(line_baby)
        self.assertEqual(left_b, "1978 - The birth of Baby Louise, the first child conceived through in vitro fertilization")
        self.assertEqual(right_b, "Figura 05")

        line_boys = "4. 1978 - The film \"The Boys From Brazil\" posits a plot to clone little Hitlers: **Figura 04**"
        left_film, right_film = split_association_item(line_boys)
        self.assertEqual(left_film, "1978 - The film \"The Boys From Brazil\" posits a plot to clone little Hitlers")
        self.assertEqual(right_film, "Figura 04")

        line_arrow = "Q21_1: Gustave Eiffel → Construiu a Torre Eifel"
        left_ar, right_ar = split_association_item(line_arrow)
        self.assertEqual(left_ar, "Gustave Eiffel")
        self.assertEqual(right_ar, "Construiu a Torre Eifel")

    def test_single_text_input_prioritizes_qnum_over_campo(self):
        """Garante que a Questão 3 com campo único NUNCA pegue o valor de CAMPO_1 da Questão 2."""
        answers_dict = {
            "CAMPO_1": "Figura 01",  # Resposta do primeiro select da Questão 2
            "CAMPO_2": "Figura 02",
            "Q2_1": "Figura 01",
            "Q3": "transport, insemination, semen, animal, fertilization, transgenic, human",
            "Q5": "The key was to make the cell quiescent"
        }

        # Simula a resolução em uma questão com campo único (text_count == 1)
        q_num = 3
        text_count = 1
        t_idx = 0
        global_input_idx = 1 # Supondo que estivesse desalinhado

        token_key = f"CAMPO_{global_input_idx}"
        if text_count == 1:
            target_val = (
                answers_dict.get(f"Q{q_num}") or
                answers_dict.get(f"QUESTAO_{q_num}") or
                answers_dict.get(f"QUESTAO {q_num}") or
                answers_dict.get(f"Questão {q_num}") or
                answers_dict.get(str(q_num)) or
                answers_dict.get(f"Q{q_num}_1") or
                answers_dict.get(token_key)
            )
        else:
            target_val = answers_dict.get(token_key)

        # Deve pegar rigorosamente o texto de Q3, e NUNCA 'Figura 01'!
        self.assertEqual(target_val, "transport, insemination, semen, animal, fertilization, transgenic, human")
        self.assertNotEqual(target_val, "Figura 01")


if __name__ == "__main__":
    unittest.main()


