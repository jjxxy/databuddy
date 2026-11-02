import {defineConfig} from 'vite';
import {fileURLToPath} from 'node:url';
export default defineConfig({define:{'process.env.NODE_ENV':'"production"'},root:fileURLToPath(new URL('.',import.meta.url)),build:{outDir:'../agent_service/static/react-dag',emptyOutDir:true,target:'es2022',cssCodeSplit:false,lib:{entry:'src/web-dag.jsx',name:'NexusWebDag',formats:['iife'],fileName:()=> 'web-dag.js',cssFileName:'web-dag'}}});
