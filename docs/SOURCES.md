# 來源與驗證範圍

資料來源查核記錄為 2026-10-11；本地基線整理日期為 2026-10-12。以下區分先前取得原檔的來源證據與目前可重跑的本地檢查，不把歷史成功當成本機已有原始資料。

## 固定資料來源

- Melops v2.0：<https://zenodo.org/records/17404087>，特定版本 DOI `10.5281/zenodo.17404087`。
- 資料論文：*A wild fish image dataset for individual re-identification and phenotyping*，Scientific Data 13, 708（2026），DOI `10.1038/s41597-026-07045-1`，<https://www.nature.com/articles/s41597-026-07045-1>。
- 作者：Tonje Knutsen Sørdalen、Ketil Malde、Canelle Sauvaitre、Anne Berit Skiftesvik、Cigdem Beyan、Torkel Larsen、Kim Tallaksen Halvorsen。
- 授權：先前官方 record API 回覆 `access_right=open`、`license.id=cc-by-4.0`；[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)。分享衍生資料時保留作者、特定版本來源、授權與變更說明。程式碼授權尚未選定。

固定取得規格保存在 `docs/source.json`。官方 metadata URL 為：

`https://zenodo.org/api/records/17404087/files/Melops_metadata.txt/content`

| 檔案 | 大小 | 已記錄的官方 MD5 |
|---|---:|---|
| Melops_metadata.txt | 3,003,668 bytes | `b91b37f4db6963896d5068f002c8222c` |
| readme.pdf | 293,585 bytes | `1c3cbeb6fb8100fcf9af1d4bf34c54fc` |

已取得並驗證過的 metadata SHA-256 為 `1aff6010f232addbf33f10a90553be5292b8755839fe904f5e896bc5fa483741`。原始 readme 引用舊紀錄 17099925，本專案仍固定以實際取得的 17404087 v2.0 為來源。

`metadata.py fetch` 只下載此 metadata，最多 5,000,000 bytes，寫入前核對 MD5；不取得 readme 或圖包。原始表包含精確座標，不隨 Git 提供。先前來源查核的 `acquisition` 設定不是本機檔案存在性的保證。

## 先前對原始 metadata 的查核

UTF-8 TSV，24 欄，24,578 列及唯一照片鍵。共有 9,861 個來源 ID 標籤，其中 9,846 個 PIT ID、15 個未標記標籤；後者不能視為 PIT 確認的身份。按 `(ID, date, sightingnumb)` 計算為 12,197 個觀察鍵，1,882 個來源 ID 有多次觀察。

4 組同 ID 同日不同序號分開保留其量測並標記 review warning，不宣稱已證明當日獨立重捕。357 個觀察鍵有同側多照片，全部保留。未發現同序號跨日期或觀察內特徵衝突；4 筆 length 缺失保持未知。性別驗證旗標為 1 的照片列為 5,138 筆，未驗證者為 19,440 筆。

上述數字是先前固定 checksum 原檔的驗證結果，不是此次離線測試重新計算。來源 length 是現場全長、單位 mm；頭部框為 pixels；未驗證的性別外觀可能具有混淆，不能以照片或模型信心補成生物學驗證。

## 本地基線可驗證範圍

原始 metadata 未隨本地基線移轉。離線初始化及 16 項合成契約／回歸測試已通過；`dev.py check` 的真實資料步驟因此明確 SKIP。使用者另行取得固定原檔後，可用相同指令重驗 checksum、分組與敏感欄位，不覆寫既有衍生結果。

資料關聯小樣本與 metadata pilot 是先前衍生物。pilot 保留 15 PIT／20 觀察／40 個候選照片列，這批候選照片尚未取得；它不是獨立效能評估集。

## 獨立的 12-PIT 頭部照片材料

本地另有 12 張頭部裁切、12 個不同來源 PIT 的審閱包，與 metadata pilot 的 PIT 集合無重疊。左右側各 6 張，2018 年 10 張、2020 年 2 張；依來源 sex × side 每格選兩個，屬方便樣本，不具代表性。來源 PIT 來自檔名與 metadata 的唯一配對，不是從像素讀出的身份。

圖片來自官方 `Melops_head.zip`。取樣環境記錄為選取 ZIP 成員核對 CRC32、移除 JPEG APP1–APP15／COM metadata，並確認去除前後解碼 RGB 一致；未保留原始 JPEG 供此次重做該比較。本地已重驗交付檔 SHA-256、完整解碼、尺寸、EXIF 缺失、CSV 與隨包 manifest 的內部配對，並檢視聯絡圖。完整官方 metadata 的獨立重配對、原始與去 metadata 圖片的 RGB 比較未在本地重做。

本地工作副本排除 `source_area` 與 manifest 中的 `raw_metadata.area`，並重新計算工作副本校驗碼；原始證據留在被 Git 忽略的資料目錄。照片、CSV、ZIP、轉移紀錄與本機路徑資訊不進入此提交。

全部人工欄仍未確認。頰部線紋及眼周表觀色相是候選，不是金標準；沒有可用尺標，不推論絕對體長、性別或全身形態。詳見 [覆核協定](REVIEW_PROTOCOL.md)。

## 大型檔案邊界

先前官方清單的頭部圖包為 5,897,145,860 bytes、原圖包為 65,103,883,600 bytes，Fish_faces_export.zip 為 714,708,432 bytes。初始化不下載這些圖包，未驗證完整圖包 MD5。少量圖片取得需另行界定範圍與總量，不能在 Range 失敗時退回整包下載。
