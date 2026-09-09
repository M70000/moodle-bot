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


if __name__ == "__main__":
    unittest.main()

