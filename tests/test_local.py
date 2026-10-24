"""Run with: python -m unittest discover -s tests"""
import unittest
from app import GroupParser, extract_schedule_info, get_source
from fastapi import HTTPException

class LocalTests(unittest.TestCase):
    def test_parser(self):
        p = GroupParser()
        p.feed('<a href="group1.html"> ИС-21 </a><a href="../secret.html">bad</a>')
        self.assertEqual(p.groups, [{'name':'ИС-21','file':'group1.html'}])
    def test_source_validation(self):
        self.assertEqual(get_source('raspisan'), 'raspisan')
        with self.assertRaises(HTTPException): get_source('other')
    def test_info(self):
        self.assertIn('times', extract_schedule_info('08:30-10:00'))

if __name__ == '__main__': unittest.main()
