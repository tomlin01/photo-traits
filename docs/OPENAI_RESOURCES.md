# OpenAI 官方資源盤點

查核記錄日期：**2026-10-11（UTC）**。以下整理先前對官方頁面的查核結果，本地基線整理時未重新連線核對；「文件可讀」不代表目前帳號具有模型權限、額度或可用端點。本專案尚未安裝 OpenAI SDK、配置金鑰、上傳研究資料或做 API smoke test。用途欄為 PhotoTraits 的規劃判斷，不是 OpenAI 對魚類特徵準確率的承諾。

| 主題與確切官方連結 | 查核結果／可用性限制 | PhotoTraits 用途 |
|---|---|---|
| [Images and vision](https://developers.openai.com/api/docs/guides/images-vision) | 已讀。支援視覺分析；模型可能誤判、縮放圖片、無法精確定位或計數，且不處理原始檔名與 metadata。不同模型的尺寸、detail 與費用限制需再核對。 | 日後描述可見顏色、紋理、損傷與品質；EXIF、拍攝日期與尺度另行解析/核對，不讓模型臆測。不可由無尺度照片產生真實 mm。 |
| [Responses API 官方入口說明](https://developers.openai.com/api/docs/guides/migrate-to-responses) | 已讀。官方建議新專案使用 Responses，支援文字/圖片與工具；API 層不等於 ChatGPT 完整產品介面。狀態保存、工具及模型可用性需按方案設定。 | 第一個模型基準可採無工具、單張或少量圖片的 Responses 請求；尚未實作或核准付費執行。 |
| [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) | 已讀。以支援的 JSON Schema 約束結構；須處理 refusal、未完成輸出與模型/schema 支援範圍。結構符合不證明欄位真實。 | 固定特徵欄位、可見性、unknown 與證據描述；維持 source / inferred / human-confirmed 分離。 |
| [Decisions API](https://developers.openai.com/api/docs/guides/decisions) | 已讀。public beta，目前文件列出僅 `gpt-6-luna`、專用 `POST /v1/decisions`，輸入文字/圖片。predicate 回傳機率、choice 固定選項、score 為等級索引的機率加權平均。可用性仍需帳號實測。 | 可選的品質/可見性判斷、人工複核路由；不是描述抽取或實際長度測量。score 不等於 mm，機率不取代人工基準與校準。 |
| [Model selection](https://developers.openai.com/api/docs/guides/model-selection)、[API 模型目錄](https://developers.openai.com/api/docs/models) | 選擇指南與模型目錄皆已讀。能力、成本、延遲與版本不同；本階段不鎖定模型、不以 ChatGPT 名稱推定 API model ID 或帳號可用性。 | 凍結同一按個體分組的資料集後，選少量候選比對品質/成本/延遲，記錄確切版本；重新查核才落實依賴。 |
| [Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices) | 已讀。可作評估方法參考；頁面明示 Evals 平台將於 2026-10-31 對既有使用者唯讀、預定 2026-11-30 關閉。不能直接以舊 Evals 平台作新架構基礎。 | 先採本地可重跑的評估資料與指標，含人工改判、未知/拒答、錯誤類型及成本；方法與託管產品生命周期分開。 |
| [API key safety](https://help.openai.com/en/articles/5112595-best-practices-for-api-key-safety) | 已讀。金鑰不置於瀏覽器/行動前端，不提交 Git；可用環境變數或秘密管理並監控用量。文件含列印金鑰的示範，本專案不照做，僅做不洩密的存在性確認。 | 真正接 API 前才走官方安全設定流程並確認預算/目的地；不要求貼聊天、不把金鑰放文件或 log。 |
| [Data controls](https://developers.openai.com/api/docs/guides/your-data) | 已讀。API 資料預設不用於訓練（除非選擇加入）；濫用監控 log 預設最多 30 天，另有法律/安全例外及端點 application state。ZDR/MAM 需資格與核准，不能假定 `store:false` 等於零保存。 | 外送前界定允許資料、最少欄位、去座標/敏感 EXIF、保存/刪除政策；CC BY 授權不自動代表研究資料外送已获使用者同意。 |
| [Apps SDK 舊入口](https://developers.openai.com/apps-sdk/) → [Plugins](https://developers.openai.com/plugins) | 當日實際觀察重導，Plugins 文件已讀。此為目前公開開發入口；不要按舊 Apps SDK 網址臆測現況。 | ChatGPT 內協作的一個後續選項，尚未決定採用或發布。 |
| [Add UI to your MCP server](https://developers.openai.com/plugins/build/chatgpt-ui) | 已讀。可選 UI 由 MCP 工具提供，iframe 透過 MCP Apps bridge 互動；新 UI 優先標準欄位/方法，ChatGPT 特有能力需 feature detection。工具應能在無 UI 情境工作。 | 可做對照照片、修改特徵、確認/補拍；需後續決定後端、驗證、資料保存與發布。獨立網頁也可自行建置後端接 Responses，不必綁定 ChatGPT。 |
| [Responses create API reference](https://developers.openai.com/api/reference/resources/responses/methods/create) | 保留正式參數入口；先前查核未完整獨立核對此頁參數，真正實作前須重新查核。 | 保留正式參數參考入口；真正實作請求前重查，現在以已讀的 Responses 指南作概念依據。 |
| [Production best practices](https://developers.openai.com/api/docs/guides/production-best-practices) | 已讀；涵蓋金鑰保護、開發/正式隔離與用量管理。此階段不建立部署或遠端環境。 | 模型階段前確認費用控制與錯誤處理，介面發布前重查安全與運維要求。 |
| [Pricing](https://developers.openai.com/api/docs/pricing) | 官方頁可讀；價格與模型/輸入/工具有關，不在此保證固定單張成本。 | 每次決定付費實驗前，以確定的圖片尺寸/detail/輸出長度重估，限制批量並追蹤實際用量。 |
| [AGENTS.md 官方指引](https://learn.chatgpt.com/docs/agent-configuration/agents-md) | 已讀；用於 repository agent instructions，具作用範圍與指令層級，不取代使用者授權。 | 保持根目錄 AGENTS 簡潔、命令可執行；改變協作規範時重查。 |

## Intelligent UI 與可開發介面的界線

使用者以 ChatGPT 的 Intelligent UI 體驗作為構想來源。這不代表本專案已取得 ChatGPT 內部元件、內建能力或某個公開同名 SDK。當日核對的公開路徑是 Responses/Decisions API 與 Plugin/MCP Apps UI；未核對到可直接依賴的「Intelligent UI SDK」，因此不建立同名依賴，也不宣稱能複製 ChatGPT 全部行為。

## 查核邊界與後續更新

以上是有日期的文件查核記錄；Responses create 的完整參數尚待實作前核對。未確認本帳號實際模型權限、速率／額度、付費執行，以及尚未選定模型的完整限制。進入模型或 UI 階段時，重查相關官方頁面、模型版本、價格、資料政策與產品棄用資訊，記錄日期後再決定依賴。此基線沒有觸發模型 API 或研究資料上傳。
