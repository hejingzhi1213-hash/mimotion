"""GitHub workflow runner; accepts legacy CONFIG and the web manager's v2 CONFIG.
No credential data is accepted as workflow inputs or emitted to logs.
"""
import contextlib
import io
import json
import os
import random
import re
import sys
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

def integer(value):
    if not re.fullmatch(r'\d{1,6}',str(value)) or not 1<=int(value)<=100000:
        raise ValueError('invalid step count')
    return int(value)

def parse_accounts(config):
    if not isinstance(config,dict):raise ValueError('config must be object')
    if config.get('version')==2:
        rows=config.get('accounts')
        if not isinstance(rows,list):raise ValueError('accounts missing')
    else:
        if not isinstance(config.get('USER'),str) or not isinstance(config.get('PWD'),str):raise ValueError('missing credentials')
        users=config['USER'].split('#');passwords=config['PWD'].split('#')
        if len(users)!=len(passwords):raise ValueError('account/password mismatch')
        rows=[dict(id='legacy-'+str(i+1),username=u,password=p,
                   min_step=config.get('MIN_STEP',18000),max_step=config.get('MAX_STEP',25000),enabled=True)
              for i,(u,p) in enumerate(zip(users,passwords))]
    accounts=[]
    for i,row in enumerate(rows):
        if not isinstance(row,dict):raise ValueError('invalid account')
        username=str(row.get('username','')).strip();password=str(row.get('password',''))
        if not username or not password:raise ValueError('empty credentials')
        low=integer(row.get('min_step',18000));high=integer(row.get('max_step',25000))
        if low>high:raise ValueError('invalid range')
        aid=str(row.get('id','account-'+str(i+1)))
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',aid):raise ValueError('invalid account id')
        if not isinstance(row.get('enabled',True),bool):raise ValueError('invalid enabled flag')
        accounts.append({**row,'id':aid,'username':username,'password':password,'min_step':low,'max_step':high,'enabled':row.get('enabled',True)})
    if len({a['id'] for a in accounts})!=len(accounts):raise ValueError('duplicate IDs')
    return accounts

def plan_runs(accounts,ids,steps,hour):
    if not isinstance(ids,list) or any(not isinstance(x,str) for x in ids):raise ValueError('invalid selection')
    if steps is not None:steps=integer(steps)
    active={a['id'] for a in accounts if a['enabled']}
    if any(x not in active for x in ids):raise ValueError('unknown or disabled selection')
    selected=[a for a in accounts if a['enabled'] and (not ids or a['id'] in ids)]
    rate=min(max(hour,0)/22,1)
    return [{**a,'steps':steps if steps is not None else random.randint(int(rate*a['min_step']),int(rate*a['max_step']))} for a in selected]

def execute_job(job):
    from util import zepp_helper as helper
    user=job['username'];user=user if user.startswith('+86') or '@' in user else '+86'+user
    # Upstream helpers can print full responses on token parsing errors: suppress them.
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
        access,error=helper.login_access_token(user,job['password'])
        if not access:return False,'login_failed'
        login,app_token,uid,error=helper.grant_login_tokens(access,str(uuid.uuid4()),user.startswith('+86'))
        if not login or not app_token or not uid:return False,'token_failed'
        device=helper.get_user_device_id(app_token,uid)
        ok,message=helper.post_fake_brand_data(str(job['steps']),app_token,uid,device)
        return bool(ok),'success' if ok else 'submit_failed'

def main():
    try:
        accounts=parse_accounts(json.loads(os.environ.get('CONFIG','{}')))
        ids=json.loads(os.environ.get('ACCOUNT_IDS','[]') or '[]')
        fixed=os.environ.get('FIXED_STEPS','').strip()
        now=datetime.now(ZoneInfo('Asia/Shanghai'))
        jobs=plan_runs(accounts,ids,integer(fixed) if fixed else None,now.hour+now.minute/60)
    except (ValueError,TypeError,KeyError):
        print('配置或刷新参数不正确，请在管理页检查后重新同步。');return 1
    if os.environ.get('DRY_RUN','false').lower()=='true':
        print('配置验证通过；账号总数：%d，计划执行：%d；未登录账号、未提交步数。'%(len(accounts),len(jobs)))
        for job in jobs:
            print('MIMOTION_PLAN '+json.dumps({'id':job['id'],'steps':job['steps']}))
        return 0
    # All requests must have finite timeouts, including upstream calls without them.
    import requests
    original=requests.sessions.Session.request
    def bounded_request(self,*args,**kwargs):
        kwargs.setdefault('timeout',(10,25));return original(self,*args,**kwargs)
    requests.sessions.Session.request=bounded_request
    success=0
    for i,job in enumerate(jobs):
        try:ok,code=execute_job(job)
        except requests.RequestException:ok,code=False,'network_error'
        except Exception:ok,code=False,'api_error'
        success+=int(ok)
        print('MIMOTION_RESULT '+json.dumps(dict(id=job['id'],steps=job['steps'],success=ok,code=code)),flush=True)
        if i+1<len(jobs):time.sleep(5)
    print('执行账号总数%d，成功：%d，失败：%d'%(len(jobs),success,len(jobs)-success))
    return 0 if success==len(jobs) else 1
if __name__=='__main__':sys.exit(main())
