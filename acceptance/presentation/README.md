# 财经演示文稿质量样例

本目录提供合成数据的可复现实测样例，不代表真实模型、真实财务口径或任意 PPT 模板验收通过。样例使用十进制计算合计，并用独立的结构检查核对原始文字、表格、图表缓存及嵌入的图表数据工作簿。

在仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe -m acceptance.presentation.finance_quality "$env:TEMP\atlas-presentation-quality.pptx"
$env:NEXUS_BROWSER_EXECUTABLE='C:\Program Files\Google\Chrome\Application\chrome.exe'
node tests/ui/presentation-quality-browser.mjs "$env:TEMP\atlas-presentation-quality.pptx" "$env:TEMP\atlas-presentation-quality-pages"
.\.venv\Scripts\python.exe -m pytest tests/test_presentation_quality.py tests/test_office_presentation_layout.py -q
```

逐页检查中文、标题、金额、单位、表格分页和图表标签。浏览器渲染只用于辅助检查，复杂图形与字体可能与 PowerPoint 不同；正式交付前仍应以实际部署的 Office 引擎和用户模板复验。自动检查不构成视觉验收或业务核准。
