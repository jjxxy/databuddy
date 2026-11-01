// Structural and CSS checks only: jsdom does not perform browser layout.
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');
const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'));
try {
 const d=dom.window.document,css=fs.readFileSync(path.join(root,'style.css'),'utf8'),style=d.createElement('style');style.textContent=css;d.head.append(style);
 const sidebar=d.querySelector('#sidebar'),scroll=d.querySelector('.sidebar-scroll'),projects=d.querySelector('#project-list'),sessions=d.querySelector('#session-home'),bottom=d.querySelector('.sidebar-bottom');
 assert(sidebar.contains(d.querySelector('#new-session')));
 assert(!sessions.contains(d.querySelector('#new-session')),'new chat stays available at the top');
 assert.equal(projects.parentElement,scroll);assert(scroll.contains(sessions));
 assert(!scroll.contains(bottom),'account and settings remain outside the scrolling list');
 assert.equal(d.querySelector('#session-filter').getAttribute('aria-label'),'筛选当前项目对话');
 const computed=el=>dom.window.getComputedStyle(el);
 assert.equal(computed(scroll).overflowY,'auto');assert.equal(computed(scroll).minHeight,'0');
 assert.equal(computed(projects).maxHeight,'min(34vh,260px)');assert.equal(computed(projects).overflowY,'auto');assert.equal(computed(d.querySelector('#session-list')).overflow,'visible');
 assert(css.includes('.resource-more:focus-visible'));assert(css.includes('@media(max-width:600px)'));
 assert(css.includes('text-overflow:ellipsis'));assert(css.includes('opacity:1'));
 console.log(JSON.stringify({separate_project_conversation_sections:true,project_list_has_independent_scroll:true,overall_sidebar_scroll:true,settings_outside_scroll:true,scoped_search_label:true,styles_focus_narrow_long_titles:true,real_browser_layout_verified:false}));
} finally {dom.window.close()}
