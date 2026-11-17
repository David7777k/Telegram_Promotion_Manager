import unittest

from promotion_studio.spintax import (COUNT_LIMIT, SpintaxError, combination_count,
                                     convert_legacy, generate, parse)


class SpintaxTests(unittest.TestCase):
    def test_nested_variants_and_literal_values(self):
        source = r"{Привет|{Добрый день|Вітаю}} \| {{channel_name}} 📚"
        values = {"channel_name": "{текст|как есть}"}
        result = generate(source, values, 10, seed=13)
        self.assertEqual(len(result), 3)
        self.assertEqual(result, generate(source, values, 10, seed=13))
        self.assertTrue(all(item.endswith("| {текст|как есть} 📚") for item in result))

    def test_malformed_templates_and_positions(self):
        for invalid in ("{x}", "{x|}", "{x| ", "{{unknown}}", "{a|{b|}}", "a|b", "\\x"):
            with self.subTest(invalid=invalid), self.assertRaises(SpintaxError):
                parse(invalid)
        with self.assertRaises(SpintaxError) as error:
            generate("line\n{valid|{{price}}}", {}, seed=0)
        self.assertEqual(error.exception.line, 2)

    def test_complexity_and_legacy(self):
        self.assertEqual(combination_count(parse("{a|b}" * 30)), COUNT_LIMIT)
        with self.assertRaises(SpintaxError):
            parse("{x|" * 17 + "y" + "}" * 17)
        with self.assertRaises(SpintaxError):
            generate("{{price}}", {"price": "x" * 17000})
        source = r"{channel} / {price} Stars / {свой текст|другой} \ {link}"
        values = {"channel_name": "Канал", "price": "250", "channel_link": "https://t.me/example"}
        self.assertEqual(generate(convert_legacy(source), values),
                         (r"Канал / 250 Stars / {свой текст|другой} \ https://t.me/example",))

