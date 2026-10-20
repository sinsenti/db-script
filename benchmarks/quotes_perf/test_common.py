"""Offline regressions for benchmark result validation. No database access."""
import decimal
import unittest
from common import canon_cell,digest_result,exact_loads,exact_dumps,loads,JSON_NULL

class CanonicalizationTests(unittest.TestCase):
    def test_extra_and_missing_cells_are_rejected(self):
        for row in [[],[1,2]]:
            with self.assertRaises(ValueError):digest_result({'headers':['a'],'rows':[row]},[{'name':'a','oid':23}])
    def test_text_does_not_unwrap_scalar_or_byte_tags(self):
        for tag in [{' $type':'uuid','value':'00123'},{'$type':'uuid','value':'00123'},{'$bytes':'00ff'}]:
            self.assertNotEqual(canon_cell(tag,25),canon_cell('00123',25))
        self.assertEqual(canon_cell({'$type':'uuid','value':'A-B'},2950),canon_cell('a-b',2950))
    def test_decimal_json_transport_is_exact(self):
        text='{"value":1.0000000000000000001,"large":9007199254740993}'
        value=exact_loads(text);self.assertEqual(value['value'],decimal.Decimal('1.0000000000000000001'))
        self.assertEqual(exact_loads(exact_dumps(value)),value)
        self.assertNotEqual(canon_cell(value,3802),canon_cell(exact_loads('{"value":1.0,"large":9007199254740993}'),3802))
        self.assertNotEqual(canon_cell(value,3802),canon_cell({'value':float(value['value']),'large':9007199254740993},3802))
    def test_json_numbers_bool_null_and_literal_tags(self):
        self.assertEqual(canon_cell({'n':decimal.Decimal('1.25')},3802),canon_cell({'n':1.25},3802))
        self.assertNotEqual(canon_cell({'n':True},3802),canon_cell({'n':1},3802))
        self.assertNotEqual(canon_cell(None,3802),canon_cell(JSON_NULL,3802))
        literal={'$type':'json','value':None};escaped={'$type':'json','value':literal}
        self.assertNotEqual(canon_cell(literal,3802),canon_cell(escaped,3802))
        self.assertIs(loads('null'),JSON_NULL)
    def test_numeric_scale_normalization_retains_precision(self):
        self.assertEqual(canon_cell({'$type':'decimal','value':'1.00'},1700),canon_cell(decimal.Decimal('1.0'),1700))
        self.assertNotEqual(canon_cell('1234567890123456789.12345678901234567890',1700),canon_cell('1234567890123456789.12345678901234567891',1700))
if __name__=='__main__':unittest.main()
