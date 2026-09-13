from hashlib import sha256
import os
from pathlib import Path
import tempfile
import unittest
from multi_shadow_clone.execution.infrastructure.owned_files import OwnedFiles
from multi_shadow_clone.execution.domain.admission import Rejected


class OwnedFilesTest(unittest.TestCase):
    def test_bounded_read_content_conflict_and_closed_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root/'a').write_text('hello')
            reader = OwnedFiles(root, {'a'})
            try:
                expected = [{'path':'a','expected_hash':sha256(b'hello').hexdigest()}]
                self.assertEqual(reader.read_files(expected,5)[0]['content'],'hello')
                with self.assertRaises(Rejected): reader.read_files(expected,4)
                (root/'a').write_text('other')
                with self.assertRaises(Rejected): reader.read_files(expected,5)
                reader.close()
                with self.assertRaises(Rejected): reader.read_files(expected,5)
            finally: reader.close()

    def test_link_special_file_and_directory_swap_do_not_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base/'owned';root.mkdir()
            outside = base/'outside';outside.mkdir()
            (outside/'secret').write_text('not allowed')
            (root/'part').mkdir()
            (root/'part'/'secret').write_text('allowed')
            reader = OwnedFiles(root, {'part/secret','link','hard','fifo'})
            try:
                (root/'part').rename(root/'old')
                (root/'part').symlink_to(outside, target_is_directory=True)
                (root/'link').symlink_to(outside/'secret')
                os.link(outside/'secret',root/'hard')
                os.mkfifo(root/'fifo')
                for path in ('part/secret','link','hard','fifo'):
                    with self.subTest(path=path), self.assertRaises((Rejected,OSError)):
                        reader.read_files([{'path':path,'expected_hash':sha256(b'not allowed').hexdigest()}],100)
                self.assertEqual((outside/'secret').read_text(),'not allowed')
            finally: reader.close()

    def test_close_waits_for_active_read_then_blocks_new_reads(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root/'input').write_text('sample')
            reader = OwnedFiles(root, {'input'})
            entered, release, closing = Event(), Event(), Event()
            original_read = os.read
            expected = [{'path':'input','expected_hash':sha256(b'sample').hexdigest()}]
            def paused_read(fd, count):
                entered.set()
                if not release.wait(5): raise TimeoutError('test release missing')
                return original_read(fd,count)
            def close():
                closing.set()
                reader.close()
            try:
                with patch('multi_shadow_clone.execution.infrastructure.owned_files.os.read',paused_read):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        future = pool.submit(reader.read_files, expected, 6)
                        try:
                            self.assertTrue(entered.wait(5))
                            stopped = pool.submit(close)
                            self.assertTrue(closing.wait(5))
                            self.assertFalse(stopped.done())
                        finally:
                            release.set()
                        self.assertEqual(future.result(timeout=5)[0]['content'],'sample')
                        stopped.result(timeout=5)
                with self.assertRaises(Rejected): reader.read_files(expected,6)
            finally:
                release.set()
                reader.close()

    def test_provisional_writer_creates_updates_rejects_stale_and_deletes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            writer = OwnedFiles(root,{'file'})
            try:
                first = writer.apply_change('file',None,'first',100)
                self.assertEqual((root/'file').read_text(),'first')
                with self.assertRaises(Rejected): writer.apply_change('file',None,'overwrite',100)
                second = writer.apply_change('file',first['after_hash'],'second',100)
                with self.assertRaises(Rejected): writer.apply_change('file',first['after_hash'],'stale',100)
                self.assertEqual((root/'file').read_text(),'second')
                self.assertFalse(list(root.glob('.multi-shadow-clone-*')))
                writer.apply_change('file',second['after_hash'],None,100)
                self.assertFalse((root/'file').exists())
            finally: writer.close()

    def test_writer_verifies_same_parent_when_directory_is_swapped(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root/'part').mkdir()
            (root/'part'/'file').write_text('actual')
            writer = OwnedFiles(root,{'part/file'})
            original_stat = os.stat
            swapped = []
            def swap_after_stat(path, *args, **kwargs):
                result = original_stat(path,*args,**kwargs)
                if path == 'file' and not swapped:
                    swapped.append(True)
                    (root/'part').rename(root/'old')
                    (root/'part').mkdir()
                    (root/'part'/'file').write_text('expected')
                return result
            try:
                with patch('multi_shadow_clone.execution.infrastructure.owned_files.os.stat',swap_after_stat):
                    with self.assertRaises(Rejected):
                        writer.apply_change('part/file',sha256(b'expected').hexdigest(),'replacement',100)
                self.assertEqual((root/'old'/'file').read_text(),'actual')
                self.assertEqual((root/'part'/'file').read_text(),'expected')
                self.assertFalse(list(root.rglob('.multi-shadow-clone-*')))
            finally: writer.close()

    def test_distinct_adapters_cannot_both_write_same_before_hash(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root/'output').write_text('before')
            writers = [OwnedFiles(root,{'output'}),OwnedFiles(root,{'output'})]
            gate = Barrier(2)
            def write(pair):
                index,writer = pair
                gate.wait(timeout=5)
                try:
                    writer.apply_change('output',sha256(b'before').hexdigest(),str(index),100)
                    return True
                except Rejected:
                    return False
            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    outcomes = list(pool.map(write,enumerate(writers)))
                self.assertEqual(sum(outcomes),1)
                self.assertEqual((root/'output').read_text(),str(outcomes.index(True)))
                self.assertFalse(list(root.glob('.multi-shadow-clone-*')))
            finally:
                for writer in writers: writer.close()

    def test_workspace_contract_reopen_and_replacement_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()/'workspace'
            root.mkdir()
            first=OwnedFiles(root,{'a'})
            original=first.contract()
            first.close()
            with self.assertRaises(Rejected):first.contract()
            reopened=OwnedFiles(root,{'a'})
            self.assertEqual(reopened.contract(),original)
            root.rename(root.with_name('previous'))
            root.mkdir()
            replacement=OwnedFiles(root,{'a'})
            try:
                self.assertNotEqual(replacement.contract(),original)
                self.assertEqual(reopened.contract(),original)
            finally:
                reopened.close();replacement.close()
