import {defineConfig} from 'vite';
import {fileURLToPath} from 'node:url';
export default defineConfig({root:fileURLToPath(new URL('.',import.meta.url)),build:{outDir:'../agent_service/static/docx-bundle',emptyOutDir:true,target:'es2022',lib:{entry:'src/docx-sandbox.js',name:'DocxPreviewSandbox',formats:['iife'],fileName:()=> 'docx-sandbox.js'}}});
