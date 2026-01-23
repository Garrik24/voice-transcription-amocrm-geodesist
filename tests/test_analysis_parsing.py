import unittest


class TestAnalysisParsing(unittest.TestCase):
    def test_normalize_list_field_from_list(self):
        from services.analysis import _normalize_list_field

        self.assertEqual(_normalize_list_field([" a ", "b", "", "  "]), ["a", "b"])

    def test_normalize_list_field_from_string(self):
        from services.analysis import _normalize_list_field

        s = "- один\n- два\n\n• три\n1) четыре\n2. пять"
        self.assertEqual(_normalize_list_field(s), ["один", "два", "три", "четыре", "пять"])

    def test_normalize_list_field_from_none(self):
        from services.analysis import _normalize_list_field

        self.assertEqual(_normalize_list_field(None), [])


class TestNoteFormatting(unittest.TestCase):
    def test_format_note_includes_sections(self):
        from services.analysis import CallAnalysis, AnalysisService

        analysis = CallAnalysis(
            client_name="Клиент",
            manager_name="Менеджер",
            summary="Обсудили задачу. Договорились о следующем.",
            client_city="Не указано",
            work_type="Консультация",
            cost="Не обсуждали",
            payment_terms="Не обсуждали",
            call_result="Перезвонить",
            next_contact_date="Не указано",
            next_steps=["Отправить КП", "Договориться о времени выезда"],
        )

        service = AnalysisService()
        note = service.format_note(analysis)

        self.assertIn("🎙️ АНАЛИЗ ЗВОНКА", note)
        self.assertIn("Суть:", note)
        self.assertIn("📍 Город:", note)
        self.assertIn("📊 Итог:", note)
        self.assertIn("✅ Следующие шаги:", note)


if __name__ == "__main__":
    unittest.main()

