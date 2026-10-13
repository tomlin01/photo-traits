# 欄位字典：固定來源查核記錄

以下語義與計數來自先前固定原檔的來源查核；原始 metadata 不隨 Git 提供。本地缺檔時 dev.py check 明確 SKIP 真實資料步驟，不代表重新驗證本表計數。提到的衍生字典亦需在取得原檔後另行產生。

已核對固定 MD5 的 3,003,668 bytes 原檔：UTF-8 TSV，24,578 列、24 欄，`head_w` 與 `head_h` 確實分欄；實際順序為 side、blurry、head_w、head_h。下表语義來自官方 record／readme，實際 header 與缺值數見 `data/derived/melops/field_dictionary.json`。未知欄位仍標記 undocumented。

實檔缺值僅觀察到 `NA`：length 4 列、trapID 1,780 列；其餘欄位無目前辨識的缺值。sex 為 f/m/s；validated_sex 為 0/1，其中 19,440 列未驗證，不能把來源性別當作已驗證答案。來源 length 是實測 mm，head_w/head_h 為像素，缺值不可補零或推算。

來源：https://zenodo.org/records/17404087 ，查核日 2026-10-11。

| 欄位 | 文件型別／單位 | 證據類型 | 語義 |
|---|---|---|---|
| `filename_year` | string | source_identifier | 影像識別碼（檔名與年份）；不是個體 ID。 |
| `date` | date dd.mm.yyyy | field_record | 捕捉日期。 |
| `year` | integer | field_record | 捕捉年份。 |
| `dayseq` | integer | source_derived | 來源定義的連續日序；不可自行假定日期基準。 |
| `ID` | string | source_identifier | PIT 或未標記個體的來源識別碼；保留字串。 |
| `tagged` | 0/1 | field_record | 是否有 PIT 標籤。 |
| `suspected_tagloss` | 0/1 | field_record | 是否疑似掉標。 |
| `length` | number mm | physical_measurement | 現場量測全長；不是從影像推算。 |
| `sex` | f/m/s | source_label | 來源性別／雄性型標籤；不能直接視為已驗證性別。 |
| `validated_sex` | 0/1 | human_validation | 來源的性別驗證旗標；不是本專案複驗。 |
| `sightings` | integer | source_derived | 來源記錄的個體總觀察次數。 |
| `sightingnumb` | integer | source_derived | 個體內觀察序號。 |
| `spawning` | 0–2 | field_record | 配子釋放指標。 |
| `cryptocotyle` | 0–3 | field_record | 寄生蟲感染指標。 |
| `scaleloss` | 0–3 | field_record | 掉鱗程度指標。 |
| `zombie` | 0–3 | field_record | 來源稱 zombie 的皮膚狀態指標；非本專案診斷。 |
| `area` | string | field_record | 來源採樣區域；目前不輸出於小樣本。 |
| `trapID` | string | sensitive_location | 捕捉設備識別碼；目前不輸出。 |
| `lat` | number degrees | sensitive_location | 精確緯度；不可出現在衍生公開成果。 |
| `lon` | number degrees | sensitive_location | 精確經度；不可出現在衍生公開成果。 |
| `side` | left/right | inference_human_review | 拍攝側別；論文描述以位置推導並人工檢查。 |
| `head_w` | number pixels | source_model_output | 頭部框寬度；像素不是 mm/cm。 |
| `head_h` | number pixels | source_model_output | 頭部框高度；像素不是 mm/cm。 |
| `blurry` | 0/1 | source_quality_flag | 來源模糊旗標；本專案未重新驗證。 |

缺值：流程暫辨識空白、NA、N/A、NaN、nan、NULL、null；本次實檔觀察到的缺值編碼是 NA。未列舉的性別碼不強行補成 f/m/s，而保留原始值並將驗證後性別設為 unknown。觀察鍵保留 ID、date、sightingnumb；4 組同日多序號分開記錄並待複核，不按日期強行合併。
