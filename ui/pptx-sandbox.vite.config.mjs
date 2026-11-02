import {defineConfig} from 'vite';
import {fileURLToPath} from 'node:url';
export default defineConfig({root:fileURLToPath(new URL('.',import.meta.url)),build:{outDir:'../agent_service/static/pptx-bundle',emptyOutDir:true,target:'es2022',lib:{entry:'src/pptx-sandbox.js',name:'PptxPreviewSandbox',formats:['iife'],fileName:()=> 'pptx-sandbox.js'}}});
