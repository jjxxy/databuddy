'use strict';
// Run new standalone UI tests from the repository root, so adding a test does
// not silently omit it from CI. These two helpers require an explicit build or
// sample-file argument and are covered by their dedicated harnesses instead.
const fs=require('node:fs'),path=require('node:path'),{spawnSync}=require('node:child_process');
const dir=__dirname,root=path.resolve(dir,'../..');
const listed=new Set([...require('./package.json').scripts.test.matchAll(/node ([\w.-]+\.cjs)/g)].map(m=>m[1]));
const helpers=new Set(['lineage-render.cjs','pptx-sandbox.cjs']);
const files=fs.readdirSync(dir).filter(name=>name.endsWith('.cjs')&&!listed.has(name)&&!helpers.has(name)).sort();
for(const name of files){const result=spawnSync(process.execPath,[path.join(dir,name)],{cwd:root,stdio:'inherit'});if(result.error)throw result.error;if(result.status!==0)process.exit(result.status||1)}
console.log(JSON.stringify({additional_ui_tests:files.length,requires_separate_harness:[...helpers]}));
