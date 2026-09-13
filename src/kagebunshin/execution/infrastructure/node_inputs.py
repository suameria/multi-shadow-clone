"""Host-authored CommonJS entry with an explicit, hash-verified input manifest."""
import json

from ..domain.admission import Rejected, content_hash, relative_path


BOOTSTRAP=r'''
const fs=require('fs'),path=require('path'),crypto=require('crypto'),Module=require('module');
const manifest=JSON.parse(process.argv[1]),entry=process.argv[2],sources=new Map();
for(const [name,hash,size] of manifest){
  const filename=path.resolve('/inputs',name);
  if(!filename.startsWith('/inputs/'))throw Error('input path escaped');
  const info=fs.lstatSync(filename);
  if(!info.isFile()||info.nlink!==1||info.size!==size)throw Error('input identity changed');
  const data=fs.readFileSync(filename);
  if(data.length!==size||crypto.createHash('sha256').update(data).digest('hex')!==hash)
    throw Error('input hash changed');
  sources.set(filename,data.toString('utf8'));
}
for(const suffix of ['.js','.cjs'])Module._extensions[suffix]=(module,filename)=>{
  if(!sources.has(filename))throw Error('undeclared module');
  module._compile(sources.get(filename),filename);
};
Module._extensions['.json']=(module,filename)=>{
  if(!sources.has(filename))throw Error('undeclared JSON module');
  module.exports=JSON.parse(sources.get(filename));
};
const filename=path.resolve('/inputs',entry);
if(!sources.has(filename))throw Error('entry is not declared');
process.argv=[process.argv[0],filename];
require(filename);
'''


def node_arguments(inputs,entrypoint):
    entrypoint=relative_path(entrypoint)
    if not entrypoint.endswith(('.js','.cjs')) or not 0<len(inputs)<=256:
        raise Rejected('unsupported Node input contract')
    seen=set()
    total=0
    manifest=[]
    for path,digest,size in inputs:
        relative_path(path)
        content_hash(digest)
        if path in seen or type(size) is not int or size<0:
            raise Rejected('invalid Node input manifest')
        seen.add(path)
        total+=size
        manifest.append([path,digest,size])
    if entrypoint not in seen or total>16777216:
        raise Rejected('Node entry or input bound differs')
    arguments=('-e',BOOTSTRAP,json.dumps(manifest,separators=(',',':'),ensure_ascii=False),entrypoint)
    if sum(len(arg.encode()) for arg in arguments)>65536:
        raise Rejected('Node input manifest exceeds command bound')
    return arguments
