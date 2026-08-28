# bs-playlists

Beat Saber（Quest版 PlaylistManager 1.3系）向けに、配布プレイリストを
「hash小文字化・同一曲の難易度統合・同期URL付け替え」して再配布する仕組み。

- 取得元一覧: `sources.txt`（JBSLは月ごとに1行追加）
- 生成物: `playlists/*.bplist`（毎日06:30 JSTに自動更新）
- Quest側は各リストの同期ボタンで最新化できる
