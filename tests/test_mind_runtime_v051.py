from pathlib import Path
import types,sys,unittest, tempfile, json
from unittest.mock import AsyncMock
from pydantic import BaseModel,Field
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sdk=types.ModuleType('maibot_sdk'); sdk.API=lambda *a,**k:lambda f:f; sdk.Tool=sdk.API;sdk.HookHandler=sdk.API;sdk.Field=Field;sdk.PluginConfigBase=BaseModel;sdk.MaiBotPlugin=type('MaiBotPlugin',(object,),{})
typesmod=types.ModuleType('maibot_sdk.types');typesmod.HookMode=types.SimpleNamespace(BLOCKING='blocking');typesmod.HookOrder=types.SimpleNamespace(EARLY=0,NORMAL=1);typesmod.ErrorPolicy=types.SimpleNamespace(SKIP='skip')
sys.modules.setdefault('maibot_sdk',sdk);sys.modules.setdefault('maibot_sdk.types',typesmod)
from MaiLivingWorld import plugin
from MaiLivingWorld.mind import evidence_from_messages
OWNER='12345678';SID='private'
class Test(unittest.IsolatedAsyncioTestCase):
 async def test_runtime_reflect_and_group_isolation(self):
  with tempfile.TemporaryDirectory() as d:
   bot=plugin.MaiLivingWorld();bot.config=plugin.Config()
   bot.config.owner.enabled=True;bot.config.owner.qq=OWNER
   bot.config.mind.enabled=True;bot.config.mind.auto_reflect=True
   bot.config.mind.mode='active';bot.ctx=types.SimpleNamespace(
    paths=types.SimpleNamespace(data_dir=Path(d)),
    chat=types.SimpleNamespace(get_stream_by_user_id=AsyncMock(return_value={'session_id':SID})),
    message=types.SimpleNamespace(get_recent=AsyncMock()),
    llm=types.SimpleNamespace(generate=AsyncMock()),
    logger=types.SimpleNamespace(debug=lambda *a:None,info=lambda *a:None,warning=lambda *a:None))
   def msg(i): return {'message_id':str(i),'session_id':SID,'platform':'qq','message_info':{'user_info':{'user_id':OWNER},'group_info':None},'processed_plain_text':'我最近很喜欢研究循环神经网络RNN','account_id':'bot'}
   data=[msg(i) for i in range(9)];bot.ctx.message.get_recent.return_value=data
   ids=evidence_from_messages(data,OWNER,SID)
   note={'kind':'preference','basis':'user_explicit','text':'主人近期明确表达喜欢研究RNN','confidence':.95,'evidence_ids':[ids[0]['id']]}
   bot.ctx.llm.generate.return_value={'content':json.dumps({'notes':[note],'corrections':[]})}
   await bot._reflect_owner(); self.assertEqual(bot.ctx.llm.generate.await_count,1)
   bot._owner_events[SID]=(0,True,'private',OWNER)
   self.assertEqual(bot._mind.notes(OWNER)[0]['status'],'active')
   bot._owner_events[SID]=(plugin.time.monotonic(),True,'private',OWNER)
   self.assertIn('RNN',bot._owner_prompt_for({'session_id':SID}))
   bot._owner_group_messages[('group','m1')]=(plugin.time.monotonic(),True,OWNER)
   self.assertNotIn('RNN',bot._owner_prompt_for({'session_id':'group','reply_message_id':'m1'}))
   bot.config.mind.mode='observe'
   self.assertNotIn('RNN',bot._owner_prompt_for({'session_id':SID}))
   await bot._reflect_owner();self.assertEqual(bot.ctx.llm.generate.await_count,1)
   bot._mind.close()
if __name__=='__main__':unittest.main()
