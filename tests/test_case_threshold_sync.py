"""用例和阈值同步：仅使用临时测试集，不执行真实评测。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import openpyxl
from asset_store import save_cases, restore_cases, editable_cases, digest
from dataset_store import create_dataset, HEADERS
from dataset_thresholds import (save_dataset_thresholds, load_dataset_thresholds,
                                thresholds_path, repair_dataset_thresholds)
from toy_management import file_hash


class CaseThresholdSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / 'source.xlsx'
        book = openpyxl.Workbook()
        book.active.append(HEADERS)
        book.active.append(['基础问答', '1', '你好', '问好', '否', '', ''])
        book.active.append(['基础问答', '2', '叫什么', '回答名字', '否', '', ''])
        book.save(source)
        book.close()
        self.dataset = create_dataset('测试集', source_path=source, dataset_id='sample', root=self.root)
        self.path = self.dataset.path
        self.sidecar = thresholds_path(self.dataset, self.root)
        save_dataset_thresholds(self.dataset, {'case_thresholds': [
            {'dataset_id': 'sample', 'category': '基础问答', 'case_id': '1', 'sheet_row': 2, 'threshold': .8},
            {'dataset_id': 'sample', 'category': '基础问答', 'case_id': '2', 'sheet_row': 3, 'threshold': .7}]}, '', self.root)

    def add(self):
        rows = editable_cases(self.path)
        rows.append({'类型': '基础问答', '用例编号': '3', '消息列表': '再见', '预期结果': '道别', '是否工具调用': False})
        return save_cases(self.path, rows, digest(self.path), allowed_root=self.root)

    def test_add_preserves_thresholds_and_restore_updates_hash(self):
        backup = self.add()
        self.assertEqual(len(editable_cases(self.path)), 3)
        data = load_dataset_thresholds(self.dataset, self.root)
        self.assertEqual([r['threshold'] for r in data['case_thresholds']], [.8, .7])
        self.assertEqual(data['dataset_sha256'], digest(self.path))
        restore_cases(self.path, backup, digest(self.path), allowed_root=self.root)
        self.assertEqual(len(editable_cases(self.path)), 2)
        load_dataset_thresholds(self.dataset, self.root)
        self.assertTrue(list((self.sidecar.parent / '.backups').glob('*sample.thresholds.json')))

    def test_delete_remaps_remaining_row(self):
        rows = editable_cases(self.path)[1:]
        save_cases(self.path, rows, digest(self.path), allowed_root=self.root)
        data = load_dataset_thresholds(self.dataset, self.root)
        self.assertEqual([(r['case_id'], r['sheet_row'], r['threshold']) for r in data['case_thresholds']], [('2', 2, .7)])

    def test_sidecar_failure_rolls_back_excel(self):
        original, thresholds = self.path.read_bytes(), self.sidecar.read_bytes()
        replace = Path.replace
        def fail(source, target):
            if Path(target) == self.sidecar:
                raise OSError('模拟阈值写入失败')
            return replace(source, target)
        with patch.object(Path, 'replace', fail), self.assertRaises(OSError):
            self.add()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.sidecar.read_bytes(), thresholds)
        self.assertFalse(list(self.sidecar.parent.glob('*.edit.lock')))

    def test_stale_sidecar_blocks_save_and_can_repair_from_matching_backup(self):
        original = self.sidecar.read_bytes()
        self.add()
        self.sidecar.write_bytes(original)  # 重现旧版保存用例后阈值未同步的状态。
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, '不匹配'):
            save_cases(self.path, editable_cases(self.path), digest(self.path), allowed_root=self.root)
        self.assertEqual(before, self.path.read_bytes())
        repair_dataset_thresholds(self.dataset, file_hash(self.sidecar), self.root)
        data = load_dataset_thresholds(self.dataset, self.root)
        self.assertEqual([r['threshold'] for r in data['case_thresholds']], [.8, .7])
        self.assertEqual(len(editable_cases(self.path)), 3)


if __name__ == '__main__':
    unittest.main()
