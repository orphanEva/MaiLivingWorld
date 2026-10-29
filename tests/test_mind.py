import json, sys, time, tempfile, sqlite3, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from MaiLivingWorld.mind import MindStore, evidence_from_messages, parse_reflection

OWNER='12345678'; SID='private-1'
def msg(mid, txt, uid=OWNER, sid=SID, group=None):
    return dict(message_id=mid,session_id=sid,platform='qq',account_id='87654321',message_info={
        'group_info': {'group_id': group} if group else None, 'user_info': {'user_id':uid}},
        processed_plain_text=txt, raw_message=[])

def reflection(n, ids, basis='user_explicit',kind='preference',conf=.95, corrections=None):
    return {'content':json.dumps({'notes':[dict(kind=kind,basis=basis,text=n,confidence=conf,evidence_ids=ids)],'corrections':corrections or []},ensure_ascii=False)}

class MindTests(unittest.TestCase):
    def test_host_message_filtering_and_forged_ids(self):
        e=evidence_from_messages([msg('1','我很喜欢学习RNN'),msg('2','这是群聊',group='12345'),msg('3','旁人',uid='99999'),msg('4','大肥鱼你好',uid='87654321')],OWNER,SID)
        self.assertEqual(len(e),2)
        self.assertEqual(e[0]['role'],'owner');self.assertEqual(e[1]['role'],'bot')
        no, conflicts=parse_reflection(reflection('我喜欢学习RNN',['E000000000000000000000']),e)
        self.assertFalse(no)
        self.assertFalse(conflicts)

    def test_no_repeat_strength_and_observe_active(self):
        with tempfile.TemporaryDirectory() as t:
            store=MindStore(Path(t)/'mind.db')
            e=evidence_from_messages([msg('1','我喜欢学习RNN')],OWNER,SID)
            notes,_=parse_reflection(reflection('主人明确表示喜欢学习RNN',[e[0]['id']]),e)
            one=store.save_reflection(OWNER,'win1',notes,[],e,time.time(),20)
            self.assertEqual(one['candidates'],1)
            self.assertEqual(store.notes(OWNER)[0]['seen_count'],1)
            self.assertEqual(store.private_hint(OWNER,mode='observe'),'')
            self.assertIn('RNN',store.private_hint(OWNER,mode='active'))
            # Same source repeated in a different batch is not new evidence.
            store.save_reflection(OWNER,'win2',notes,[],e,time.time()+1,20)
            self.assertEqual(store.notes(OWNER)[0]['seen_count'],1)
            self.assertEqual(store.private_hint('99999999',mode='active'),'')
            store.close()

    def test_conflict_decision_and_rollback(self):
        with tempfile.TemporaryDirectory() as t:
            store=MindStore(Path(t)/'mind.db')
            e=evidence_from_messages([msg('1','我喜欢学习RNN'),msg('2','我现在不喜欢RNN了')],OWNER,SID)
            notes,_=parse_reflection(reflection('主人明确表示喜欢学习RNN',[e[0]['id']]),e)
            store.save_reflection(OWNER,'w1',notes,[],e,time.time(),20)
            key=store.notes(OWNER)[0]['note_key'];before=store.snapshots(OWNER)[0]['id']
            corrections=[{'target_key':key,'reason':'主人后来明确否认仍然喜欢RNN','evidence_ids':[e[1]['id']]}]
            _,corr=parse_reflection(reflection('新增证据指向旧偏好不再成立',[e[1]['id']],corrections=corrections),e)
            store.save_reflection(OWNER,'w2',[],corr,e,time.time()+2,20)
            self.assertNotIn('RNN',store.private_hint(OWNER,mode='active'))
            review=store.reviews(OWNER)[0]['id']
            self.assertFalse(store.decide('other',review,'approve'))
            self.assertTrue(store.decide(OWNER,review,'reject'))
            self.assertIn('RNN',store.private_hint(OWNER,mode='active'))
            self.assertTrue(store.restore(OWNER,before))
            self.assertEqual(store.notes(OWNER),[])
            store.close()

    def test_migration_keeps_legacy_but_never_injects(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'mind.db'
            with sqlite3.connect(path) as con:
                con.execute('CREATE TABLE mind_notes(owner_id TEXT,note_key TEXT,kind TEXT,text TEXT,basis TEXT,confidence REAL,seen_count INTEGER,first_seen REAL,last_seen REAL,PRIMARY KEY(owner_id,note_key))')
                con.execute('INSERT INTO mind_notes VALUES (?,?,?,?,?,?,?,?,?)',(OWNER,'k','preference','历史未证明的主人喜好','user_explicit',1.,30,time.time(),time.time()))
            store=MindStore(path)
            self.assertEqual(store.notes(OWNER)[0]['status'],'legacy')
            self.assertEqual(store.private_hint(OWNER,mode='active'),'')
            store.close()

if __name__=='__main__': unittest.main()
