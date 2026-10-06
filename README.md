# 稿件匿名互评

Flask + SQLite + 原生 JavaScript 实现的稿件匿名互评网页，无需注册。

## 启动（单条命令）

```bash
docker compose up --build
```

访问地址：**http://localhost:8000**

依赖安装随镜像构建完成，建库建表随容器启动自动完成（幂等）。SQLite 数据保存在命名卷 `review_data`（容器内 `/data/app.db`），重启后数据与链接均保留。

## 使用流程

1. 打开首页，录入 3～8 人的姓名与纯文本稿件，生成私密链接。
2. 主持人保存**主持链接**（`/h/<随机串>`），把各自的**个人链接**（`/p/<随机串>`）分发给对应的人。链接随机生成、无法找回，请妥善保存。
3. 每位参与者在自己的页面：修改自己的稿件、阅读分配到的稿件（按录入顺序循环：每人评下一人，末位评首位）、提交评审意见。
4. 主持台实时显示待交人员；当每篇当前稿件都有有效意见后，主持人点击"全部公布"一次性公布。
5. 公布后，各参与者在自己的页面看到收到的匿名意见，稿件与意见均不可再改。
6. **已读回执**：公布后，作者在页面底部阅读收到的匿名意见后，点击"我已阅读，确认收到此意见"明确确认；仅打开页面、轮询或提问不会自动确认。服务端记录首次确认时间，重复确认不会改动；主持台按留存名单原顺序显示各成员"已确认/待确认"与首次确认时间，不展示意见或问答内容。
7. **一问一答澄清**：公布后，作者可就自己收到的那条意见提交一次澄清问题；对应评审者在自己页面看到问题后答复一次。每条意见只容纳一问一答，提交后均不可修改；问答仅在该作者与评审者各自的参与页显示，页面继续保持匿名（不显示对方姓名或链接），主持台与其他参与者看不到任何问答内容。
8. **意见举报**：公布后，作者可对自己收到的意见提交一次举报，原因非空且不超过 1000 字。主持台只显示举报编号、原因、意见原文和处理状态（不显示作者姓名、个人链接或问答）；主持人可对每条待处理举报选择**保留**或**屏蔽**，处理后不可修改。屏蔽后，该作者参与页中的 `received` 变为 `null` 并提示已屏蔽；意见原文、问答、回执以及评审者自己的意见均不变。
9. **个人链接换发**：某条个人链接泄露时，主持人可在主持台为该成员换发链接（公布前后均可）。换发后立即生成新的随机链接并更新主持台，**原链接立即作废**：换发提交后用原链接发起的状态读取与改稿、意见、问题、答复请求一律返回 `404`，且任何响应都不会透露新链接。新链接沿用同一参与者的稿件、版本、循环分配、意见与一问一答（不重建身份、不使意见失效）；其他人的链接及公布状态均不变。
10. **公布前移除退出成员**：有人退出时，主持人可在主持台凭姓名将其移出（仅公布前，至少保留 3 人；**不支持补人或恢复**）。移除在同一事务内让名单、分配与意见一起生效：留存成员按原录入顺序重新闭合互评环（如甲、乙、丙、丁中移除乙，则甲改评丙、丙评丁、丁评甲；移除首位的甲，则由末位的丁改评乙）；退出者的个人链接**永久失效**（状态读取及一切写入一律 `404`），其发出或收到的意见都不计入公布条件。只有**原来评退出者**的人会改评下一位留存成员，其旧意见失效并拿到新的匿名分配凭据，需重新阅读后提交；其他人的稿件、版本、意见与链接全部保留。被改分配的参与者打开页面时，已输入的意见文本会保留，但提交会被禁用，确认"重新阅读"后才绑定新凭据。
11. **永久封存（公布后）**：主持人可在主持台将已公布会话**永久封存**，供双方只读查阅。仅当没有未答复的澄清问题、没有待处理举报时才能封存；未公布或仍有未解决事项返回 `409`，失败不改动数据。封存与提问、答复、举报、换发等写入按提交顺序串行生效。封存后主持台与参与页都会提示已封存并禁用一切写操作；该会话的**所有合法写请求**——包括重复确认已读、换发链接、举报裁定以及重复封存——一律返回 `409`，无效链接仍返回 `404`。封存**不删除任何数据、不撤销链接**：双方原有读取权限、匿名隔离与举报屏蔽语义全部保留，可随时只读回看；未封存会话的接口行为完全不变。封存状态保存在原数据卷中，重启后保留。
12. **匿名合辑（公布后）**：公布后，每位参与者可在自己页面**授权或撤回**本人稿件收入匿名合辑（初始未授权，换发链接不改变授权）。主持台显示各人授权状态，主持人勾选姓名（非空、不重复的留存名单）后**一次发布**：只有所有入选稿件都已授权才能成功，任一未授权则整次失败、不留合辑。发布后生成独立的随机公开链接（`/a/<随机串>`）：公开页只按编号展示稿文（编号按原名单顺序），不含姓名、意见、问答等任何其他信息。发布后全部授权冻结，不能变更、不能重复发布；会话封存后合辑公开页仍可只读访问。

## 规则与实现要点

- **匿名性**：参与页及其接口响应不包含他人姓名字段与任何私密链接；公布前作者无法读取收到的意见；公布后的意见也不含评审人信息。
- **版本绑定**：每次改稿使稿件版本号 +1，并立即使针对该稿的已交意见失效（其他人的意见不受影响）。提交意见必须绑定所读稿件的版本号，版本不符（过期提交）会被拒绝，且不会覆盖已有的有效意见。
- **公布门槛**：仅当每篇当前版本的稿件都存在有效意见时才允许公布；公布为一次性操作。退出成员发出或收到的意见一律不计入公布条件。
- **移除成员（公布前）**：`POST /api/h/<host_token>/remove`，请求体仅携带 `name`。主持链接无效或成员不存在（含已移除者，不能恢复）返回 `404`；已公布、或移除后留存成员不足 3 人返回 `409`；姓名缺失/格式不正确返回 `400`。成功时名单、分配与意见在同一写事务内一起生效：退出者置为永久退出（`active=0`，其当前链接与历史链接在所有接口上按不存在处理），留存成员按原录入顺序闭合新环；仅"原来评退出者的人"改评下一位留存成员——即原录入顺序中排在退出者前面最近的一位留存者；退出者原为当前首位时，则是留存者中录入顺序最末的一位（环闭合处"末位评首位"）。其旧意见置为失效、匿名分配凭据换发（nonce 轮换、代数 +1），其他人的稿件、版本、意见、链接与分配凭据一律不动。移除与意见提交、公布一样在 `BEGIN IMMEDIATE` 事务内"先检查后变更"，按提交顺序串行生效，任一检查失败即整体回滚、不留部分变更。
- **分配凭据**：`GET /api/p/<token>` 的 `assignment.credential` 返回与当前分配绑定、不暴露作者身份的随机凭据（响应中不含被评者姓名或链接）。提交意见时同时校验**凭据与稿件版本**：分配改变后缺少凭据或沿用旧凭据都返回 `409`（响应带 `"assignment_changed": true`），即使新、旧两篇稿件的版本号恰好相同；版本不符仍返回 `409`。分配从未变过（包括旧库升级后的既有成员）时兼容不带 `credential` 的原提交格式。参与页轮询发现凭据变化时，保留未提交的意见文本并禁用提交按钮，展示新分配稿件，参与者点击"我已重新阅读，确认评审此稿"后才在本地绑定新凭据并恢复提交。
- **一问一答澄清**：仅公布后开放。作者可对自己收到的那条意见提一次澄清问题（`POST /api/p/<作者token>/question`），对应评审者答复一次（`POST /api/p/<评审者token>/answer`）；身份与"对应哪条意见"完全由服务端凭个人链接确定，请求体只含文本。每条意见至多一问一答，问题与答复提交后均不可修改，原稿与原意见保持冻结。问答文本沿用意见的非空及长度限制（1～100000 字）。公布前提问/答复、尚无问题时答复、重复提问/重复答复均返回 `409`，失败不改变任何数据；并发提交同一问题或答复最多一次成功（写事务 `BEGIN IMMEDIATE` + `clarifications.review_id UNIQUE`）。
- **问答可见性**：问答随参与状态在 `GET /api/p/<token>` 的同一只读快照中返回：作者侧为 `received_clarification`（对应自己收到的意见），评审者侧为 `my_review_clarification`（对应自己提交的意见），公布前两者均为 `null`。结构为 `{"question": ..., "answer": ..., "answered": ...}`，不含评审者/作者姓名或任何私密链接；其他参与者与 `GET /api/h/<host_token>` 主持状态响应均不包含问答内容。参与页轮询只在快照状态变化时切换只读展示，不会覆盖正在输入的问题或答复。
- **已读回执**：公布后作者在自己页面点击"我已阅读，确认收到此意见"，或调用 `POST /api/p/<作者token>/ack`（无需请求体，服务端凭个人链接确定作者与对应意见）明确确认已读；仅打开页面、轮询或提问不会自动确认。首次成功记录确认时间，重复确认仍返回成功且首次时间不变；公布前确认返回 `409`，无效、已退出或已换发的旧个人链接返回 `404`，失败不写任何数据。作者侧 `GET /api/p/<token>` 的 `receipt` 字段（`{"acknowledged": ..., "acknowledged_at": ...}`，公布前为 `null`）只含自己的回执，与其他状态字段在同一快照读取；主持台 `GET /api/h/<host_token>` 的 `reviewers` 附带 `acknowledged` 与 `acknowledged_at`，公布后按留存名单原顺序展示，不含意见或问答内容，其他参与者无法获得别人的回执。确认只写入回执本身：稿件、意见与一问一答均不变，提问/答复权限不受影响；换发链接后回执随原身份保留。回执存于独立 `receipts` 表（`review_id` 唯一），旧库启动时自动补建，既有意见初始为待确认，重启后回执保留。
- **意见举报与屏蔽**：仅公布后开放，作者调用 `POST /api/p/<作者token>/report`，请求体只含 `reason`（非空、去掉空白后仍有内容、长度 1～1000）。服务端凭个人链接确定作者及"其收到的那条意见"，无需也不接受请求方提供意见编号；公布前或每条意见重复举报返回 `409`，无效个人链接返回 `404`，原因无效返回 `400`，失败不留变更。举报存于独立 `reports` 表，`review_id UNIQUE` 保证每条意见至多一条举报。作者在自己的参与状态 `report` 字段查看本人举报：`{"id": 编号, "reason": 原因, "status": "pending|kept|blocked"}`；未举报或公布前为 `null`，其他参与者无法获得该举报。主持台状态新增 `reports` 数组，每项只有 `id`、`reason`、`review`（意见原文）与 `status`，不含姓名、个人链接、问答或回执；无效主持链接 `404`，跨会话或不存在的编号也 `404`。主持人调用 `POST /api/h/<主持token>/reports/<编号>/keep` 或 `/block` 保留/屏蔽待处理举报；重复处理返回 `409`，写事务和条件更新保证同一举报并发处理最多一次成功，裁定写入后不可修改。屏蔽只影响该作者读取意见原文：其参与状态为 `received:null` 且 `received_blocked:true`，页面提示已屏蔽；意见原文仍在数据库和主持举报列表中，问答、回执及评审者自己的意见均不变，问题/答复权限也不变。举报记录与裁定重启后保留，旧库启动时自动补建 `reports` 表。
- **永久封存（公布后）**：`POST /api/h/<主持token>/close`，无需请求体。无效主持链接返回 `404`；未公布、存在未答澄清问题（已问未答）或待处理（`pending`）举报、或会话已封存时返回 `409`，失败不改动任何数据。封存与提问、答复、举报、换发等一切写入同在 `BEGIN IMMEDIATE` 写事务内按提交顺序串行生效：封存先提交则之后排队的写入全部按封存后状态拒绝（不会出现封存成功后仍写入新问题或新举报），写入先提交则封存时重新检查——若留下未答问题或待处理举报则封存被整体拒绝（不能封存带未解决事项的会话）。封存成功后，该会话所有合法写请求均返回 `409 {"error":"会话已封存，只读，不能再写入"}`：含改稿、提交意见、提问、答复、确认已读（**包括重复确认**）、举报、换发链接、移除成员、举报裁定与重复封存；无效（含已退出、已换发作废的）链接在各接口仍按不存在处理返回 `404`。主持状态 `GET /api/h/<主持token>` 与参与状态 `GET /api/p/<token>` 均在原快照中新增布尔字段 `closed`（与 `published` 等字段同一快照；未封存恒为 `false`），两端页面据此提示封存并禁用写操作。封存**不删除数据、不撤销链接**：稿件、意见、问答、回执、举报、主持链接与全部个人链接原样保留，原读取权限、匿名隔离（不返回他人姓名/链接）、举报屏蔽（`received:null` + `received_blocked:true`）等语义不变，双方继续只读查阅。封存状态存于 `sessions.closed` 列（旧库启动时自动 `ALTER TABLE ADD COLUMN ... DEFAULT 0`，既有会话一律未封存），随原数据卷重启保留；未封存会话的旧接口行为保持不变。
- **并发与原子性**：所有写操作（改稿、提交、公布、封存、提问、答复、换发链接、举报与举报裁定）都在 `BEGIN IMMEDIATE` 事务内"先检查、后变更"，任一规则不满足即整体回滚，拒绝的操作不留下部分变更；SQLite 开启 WAL 与 busy_timeout 以支持并发请求。
- **个人链接换发**：`POST /api/h/<host_token>/rotate-link`，请求体携带 `name`（姓名）与 `link`（主持台页面所见的个人链接，接受 `/p/<token>` 相对路径或完整 URL）。仅有效主持链接可操作本会话人员：主持链接无效或姓名不在本会话返回 `404`；请求格式不正确（缺姓名/链接、链接不是个人链接）返回 `400`；所见链接不是该成员当前链接（已过期作废的旧链接、他人链接或伪造链接）返回 `409`；失败不改变任何数据，错误响应不返回新链接。成功返回 `{"name": ..., "url": "/p/<新token>"}`，并在同一事务内把旧链接登记到 `token_history` 后把当前链接更新为新随机串；换发提交后，旧链接在 `GET /api/p/<旧token>` 及改稿、意见、问题、答复接口上均按不存在处理（`404`）。新链接复用同一 `participants` 行，稿件、版本、循环分配、意见与问答原样保留，不重建身份、不使意见失效；其他人的链接与公布状态不变。同一旧链接的并发换发最多一次成功（写事务串行化 + `token_history.token UNIQUE`）；旧链接写入与换发按提交顺序生效——换发先提交（先拿到写锁）则后到的旧凭据写入整体拒绝，旧凭据写入先提交则其修改保留、随后的换发照常成功且新链接看到的就是新版本。
- **持久化与旧库升级**：全部数据存于 SQLite 文件，重启后保留：换发后的新链接持续有效，已作废的旧链接重启后仍为失效状态；移除状态、闭合后的新环与新分配凭据重启后同样保留（退出者链接重启后仍 `404`）；已确认的回执与首次确认时间重启后保留；举报原因、编号、处理状态与保留/屏蔽裁定重启后保留；封存状态（`sessions.closed`）重启后保留，封存后的会话重启仍只读；合辑授权（`participants.anthology_consent`）、已发合辑与公开链接（`anthologies`/`anthology_items`）重启后保留，发布后的冻结状态同样保留。旧版本数据库（无问答表/历史链接表/回执表/举报表/合辑表，或 `participants` 缺 `active` / `assignment_nonce` / `assignment_gen` / `anthology_consent` 列，或 `sessions` 缺 `closed` 列）在新版本启动时自动通过 `CREATE TABLE IF NOT EXISTS` 与 `ALTER TABLE ADD COLUMN` 补建/补列，并为既有成员回填分配凭据（视为分配从未变过，兼容原提交格式）、为既有会话补 `closed=0`（一律未封存）、为既有成员补 `anthology_consent=0`（一律未授权），既有数据原样保留——旧库因此自动支持移除成员、已读回执、意见举报、会话封存与匿名合辑（既有意见初始为待确认、未举报，既有成员初始未授权），重启后状态保留；沿用现有 Docker Compose 启动方式与命名卷 `review_data`，无需手动迁移。
- **匿名合辑（公布后）**：仅已公布且未封存的会话可授权/撤回（`POST /api/p/<token>/anthology-consent`，请求体 `{"consent": true|false}`）与发布合辑（`POST /api/h/<host_token>/anthology`，请求体 `{"names": [...]}`），否则 `409`。授权绑定参与者身份（`participants.anthology_consent`），初始未授权，换发链接不改变。参与状态新增 `anthology_consent`（仅本人授权），主持状态 `reviewers` 各项新增 `anthology_consent`（各人授权）、顶层新增 `anthology_url`（未发布为 `null`），均与原字段同一快照读取；参与状态不含公开链接。名单非法（空、重复、姓名空白/超长/非字符串）返回 `400`；主持链接无效或姓名不属于本会话留存成员（含已移除者）返回 `404`；任一入选稿未授权则整次发布 `409`（响应带 `missing_consent` 名单）且不留合辑、授权不变。授权变更与发布同在 `BEGIN IMMEDIATE` 写事务内"先检查后变更"，按提交顺序串行生效：入选作者先撤回则发布看到撤回而失败；发布先提交则全部授权冻结，此后的授权变更与重复发布一律 `409`。成功时生成独立随机公开链接（`/a/<公开token>`），入选稿文复制固化、按原名单（录入）顺序从 1 编号；`GET /api/a/<公开token>` 与公开页 `/a/<公开token>` 只含编号与稿文，不含姓名、私密链接、意见、问答或授权记录，无效公开链接 `404`，会话封存后合辑仍可只读。合辑存于 `anthologies`/`anthology_items` 表（`session_id` 唯一，每会话至多一份），旧库启动自动补表补列（既有成员默认未授权），随原数据卷持久化。
- **读取语义（快照一致性）**：`GET /api/p/<token>` 与 `GET /api/h/<host_token>` 的全部查询在同一个只读事务（`BEGIN`）内完成，响应中的本人稿件、分配稿件、版本、意见、问答、举报状态、屏蔽标记、公布状态与封存标记（`closed`）来自同一快照，不会拼接不同时间点的数据。快照可能完整落在公布前（`published=false`、`received=null`、两个问答字段均为 `null`、`report=null`、`closed=false`）；公布后若收到的有效意见尚未被屏蔽，`received` 为意见原文；封存后同一快照中 `published=true` 且 `closed=true`，页面整体只读但各读取字段不变；若作者对该意见的举报已被屏蔽（屏蔽只能发生在封存前），则同一快照中 `received=null` 且 `received_blocked=true`，问答、回执和 `report` 仍随原意见返回。主持台 `reports` 同样取自该快照。WAL 模式下只读事务不阻塞其他连接的写入，写入也不阻塞读取；无论成功、链接无效（404）还是读取异常（500），请求结束都会结束事务并释放连接。

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
python app.py
```

访问 http://localhost:8000 。数据默认写入 `./data/app.db`，可用环境变量 `DB_PATH`、`PORT` 覆盖。

## 自测

```bash
python selftest.py
```

自测使用独立临时数据库（不影响 `./data`），通过可控交错验证读取语义：在读事务暂停期间，另一连接依次完成改稿、提交新版意见与公布，继续完成的读取仍返回一致的（公布前）快照，而非 `published=true` + 旧版本 + `received=null` 的拼接结果；同时覆盖成功、无效链接（404）、读取异常（500）下的事务结束与连接释放，匿名隔离、改稿失效、过期拒绝、公布门槛与公布冻结等既有规则的回归，一问一答的 409 规则、身份隔离、并发至多一次成功、快照一致与旧库自动补表，以及个人链接换发的 404/409/400 规则、旧链接在所有接口立即失效、新链接沿用稿件版本分配意见问答、并发换发至多一次成功、写入与换发的提交顺序、其他人与公布状态不变和重启持久化；以及公布前移除成员的环闭合（含移除首/末位）、退出者链接在所有接口 404 且不可再换发、仅原评审者旧意见失效并改评下一位、移除首位时仅末位改评并换发凭据（乙、丙等其他人稿件/版本/意见/链接/凭据不变，改评者缺凭据或沿用旧凭据 409 且不写入，持新凭据补齐后才可公布）、五人连续移除当前首位时凭据逐次换发、其他人稿件版本意见链接保留、404/409/400 规则、分配凭据的新旧格式兼容与同版本旧凭据 409、移除与意见提交的提交顺序、公布门槛排除退出者意见、旧库自动补列回填凭据后可移除、重启后移除状态与凭据保留；以及公布后已读回执的 404/409 规则、首次确认时间不被重复确认改动、打开页面/轮询/提问不自动确认、参与者只能获得自己的回执、主持台按留存名单原顺序显示确认状态与首次确认时间（不含意见或问答内容）、换发链接后回执随原身份保留、确认不改变稿件意见问答与提问答复权限、并发确认只写一条且全部成功、回执随状态同一快照返回、旧库自动补表且既有意见初始为待确认、重启后回执保留；以及公布后意见举报的 400/404/409 规则、每条意见只可举报一次、作者只能看到自己的举报、主持列表仅含编号/原因/意见原文/状态、保留与屏蔽后不可改、跨会话编号 404、并发裁定最多一次成功、屏蔽后 `received=null` 且 `received_blocked=true` 而问答/回执/评审者意见不变、旧库自动补表且重启后举报与裁定保留；以及公布后永久封存的无效链接 404、未公布/有未答澄清/有待处理举报/重复封存 409 而失败不改数据、问答闭环与举报裁定后可封存、封存成功后重复封存及含重复确认已读和换发链接在内的一切合法写请求一律 409 而无效链接仍 404、封存与提问/举报按提交顺序串行生效（封存先提交则排队写入整体拒绝且不写入新问题/举报，写入先提交则封存重新检查、未解决事项未闭环前封存不掉）、封存不删除数据不撤销链接且读取权限/匿名隔离/屏蔽语义不变、`closed` 与原字段同快照返回、未封存会话旧行为不变、旧库自动补 `closed` 列且既有会话默认未封存、封存状态随重启保留；以及匿名合辑的初始未授权、授权/撤回的 400/404/409 规则与公布前 409、参与状态仅本人授权与主持状态各人授权的同快照返回、非法名单 400、非留存成员（含已移除者）404、任一入选稿未授权整次 409 且不留合辑不冻结状态、按原名单顺序编号与稿文固化、公开响应仅含编号与稿文（不含姓名/私密链接/意见/问答/举报/授权记录）且未入选稿不出现、无效公开链接 404、发布后授权冻结与重复发布 409、授权变更与发布的提交顺序（撤回先提交则发布整次失败，发布先提交则变更 409 且重复发布 409）、换发链接不改变授权（旧链接授权操作 404）、封存后合辑仍可只读而授权/发布 409、旧库自动补表补列且既有成员默认未授权、合辑与授权随重启保留。

## 接口概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/sessions` | 创建会话，返回主持链接与各人个人链接 |
| GET | `/api/h/<host_token>` | 主持台状态（含待交人员、个人链接、举报列表、封存标记 `closed`、各人合辑授权与 `anthology_url`；公布后含各成员已读回执状态与首次确认时间） |
| POST | `/api/h/<host_token>/publish` | 一次性公布（不满足条件则 409 拒绝） |
| POST | `/api/h/<host_token>/anthology` | 按姓名发布一次匿名合辑（名单非空无重复；任一入选稿未授权则整次 409；发布后冻结） |
| POST | `/api/h/<host_token>/close` | 将已公布且无未解决事项的会话永久封存为双方只读（未公布/有未答问题/有待处理举报/重复封存 → 409；封存后一切写入 409） |
| POST | `/api/h/<host_token>/reports/<id>/keep` | 保留一条待处理举报（处理后不可改，重复 409） |
| POST | `/api/h/<host_token>/reports/<id>/block` | 屏蔽一条待处理举报（作者不再看到意见原文；其他内容不变） |
| POST | `/api/h/<host_token>/remove` | 公布前移除一名退出成员（至少留 3 人；退出者链接永久失效，环按原顺序闭合，仅原评审者改评下一位） |
| POST | `/api/h/<host_token>/rotate-link` | 为指定成员换发个人链接（公布前后均可；旧链接作废，新链接沿用其稿件/意见/问答/回执） |
| GET | `/api/p/<token>` | 参与页状态（匿名：无他人姓名/链接；含一问一答、自己的举报状态/屏蔽标记、匿名分配凭据 `assignment.credential`、自己的已读回执 `receipt`、封存标记 `closed` 与本人合辑授权 `anthology_consent`） |
| PUT | `/api/p/<token>/manuscript` | 改稿（版本 +1，使针对它的已交意见失效） |
| POST | `/api/p/<token>/review` | 提交意见（同时绑定所读稿件版本与分配凭据；版本过期/凭据失效 409；分配从未变过时可不带凭据） |
| POST | `/api/p/<token>/question` | 公布后作者就收到的意见提一次澄清问题（重复/公布前 409） |
| POST | `/api/p/<token>/answer` | 公布后评审者答复一次（尚无问题/已答复/公布前 409） |
| POST | `/api/p/<token>/ack` | 公布后作者确认已读收到的意见（首次记录确认时间，重复确认不改；公布前 409；无效/已退出/已换发的旧链接 404） |
| POST | `/api/p/<token>/report` | 公布后作者举报收到的意见（原因 1～1000 字；每条意见一次，重复/公布前 409，无效链接 404） |
| POST | `/api/p/<token>/anthology-consent` | 公布后授权/撤回本人稿件收入匿名合辑（`{"consent": true|false}`，初始未授权；未公布/已封存/合辑已发布 409，无效链接 404） |
| GET | `/api/a/<public_token>` | 匿名合辑公开数据（只含编号与稿文；任何人可读，封存后仍可只读；无效公开链接 404） |

### 一问一答调用示例

以下 `<作者token>`、`<评审者token>` 均为各自个人链接 `/p/<token>` 中的随机串；服务端凭链接确定身份与对应意见，请求中不需要、也不接受任何对方身份信息。

```bash
# 作者就自己收到的匿名意见提问（公布后；每条意见一次）
curl -X POST http://localhost:8000/api/p/<作者token>/question \
  -H 'Content-Type: application/json' \
  -d '{"question": "意见第三点能否举例说明？"}'
# 200 {"ok":true}；公布前、重复提问返回 409 {"error": "..."}

# 对应评审者答复（仅可答复一次，提交后不可修改）
curl -X POST http://localhost:8000/api/p/<评审者token>/answer \
  -H 'Content-Type: application/json' \
  -d '{"answer": "第三点指的是……"}'
# 200 {"ok":true}；尚无问题、已答复、公布前返回 409

# 双方在各自参与页同快照读取问答（其他参与者与主持台均不返回问答）
curl http://localhost:8000/api/p/<作者token>
# 作者侧："received_clarification": {"question": "...", "answer": "...", "answered": true}
curl http://localhost:8000/api/p/<评审者token>
# 评审者侧："my_review_clarification": {"question": "...", "answer": "...", "answered": true}
```

### 已读回执调用示例

`<作者token>` 为作者个人链接 `/p/<token>` 中的随机串；服务端凭链接确定作者与对应意见，请求无需携带任何内容。打开页面、轮询或提问都不会产生回执，只能显式确认。

```bash
# 作者确认已读自己收到的匿名意见（公布后）
curl -X POST http://localhost:8000/api/p/<作者token>/ack
# 200 {"ok":true,"acknowledged_at":"2026-10-05 08:30:00"}（首次确认时间，服务器 UTC 时间）
# 重复执行仍返回 200 且 acknowledged_at 不变；公布前返回 409；
# 无效、已退出或已换发的旧个人链接返回 404；失败不写任何数据

# 作者在参与页状态中看到"自己的"回执（与其他状态字段同一快照）
curl http://localhost:8000/api/p/<作者token>
# "receipt": {"acknowledged": true, "acknowledged_at": "2026-10-05 08:30:00"}
# 公布前为 "receipt": null；其他参与者的响应里不会出现这条回执

# 主持台按留存名单原顺序查看各成员确认状态与首次确认时间（不含意见或问答内容）
curl http://localhost:8000/api/h/<主持token>
# "reviewers": [
#   {"name":"甲","submitted":true,"acknowledged":true,"acknowledged_at":"2026-10-05 08:30:00"},
#   {"name":"乙","submitted":true,"acknowledged":false,"acknowledged_at":null}, ...
# ]
```

### 意见举报调用示例

作者个人链接中的 `<作者token>` 已由服务端关联到作者本人及公布后收到的那条意见；举报请求只需原因。原因须非空且不超过 1000 字，每条意见只能举报一次。

```bash
# 作者举报自己收到的意见（公布后）
curl -X POST http://localhost:8000/api/p/<作者token>/report \
  -H 'Content-Type: application/json' \
  -d '{"reason": "该意见包含与稿件无关的攻击内容，请主持人处理。"}'
# 200 {"ok":true,"id":12,"status":"pending"}
# 原因为空或超过 1000 字 → 400；公布前或重复举报 → 409；无效个人链接 → 404

# 作者只查看自己的举报状态；其他参与者响应中的 report 仍为 null
curl http://localhost:8000/api/p/<作者token>
# "report": {"id":12,"reason":"...","status":"pending"}

# 主持台查看本会话举报列表（只含编号、原因、意见原文、处理状态）
curl http://localhost:8000/api/h/<主持token>
# "reports": [
#   {"id":12,"reason":"...","review":"收到的意见原文","status":"pending"}
# ]

# 主持人保留或屏蔽；二者都只能对 pending 举报执行一次
curl -X POST http://localhost:8000/api/h/<主持token>/reports/12/keep
# 200 {"ok":true,"id":12,"status":"kept"}
curl -X POST http://localhost:8000/api/h/<主持token>/reports/12/block
# 若已处理：409 {"error":"该举报已经处理，不能重复处理或修改"}

# 屏蔽后，作者同一快照不再获得意见原文，但问答与回执仍在
curl http://localhost:8000/api/p/<作者token>
# "received": null,
# "received_blocked": true,
# "report": {"id":12,"reason":"...","status":"blocked"},
# "received_clarification": {...}, "receipt": {...}
# 无效主持链接或跨会话编号 → 404；并发保留/屏蔽只有一个请求成功
```

### 封存会话与拒绝写入示例

封存只需主持链接、无需请求体；只有已公布、没有未答复澄清问题且没有待处理举报的会话才能封存。封存是一次性、永久操作，但不删除任何数据或链接。

```bash
# 主持人永久封存本会话（公布后；无未答澄清、无待处理举报）
curl -X POST http://localhost:8000/api/h/<主持token>/close
# 200 {"ok":true,"closed":true}

# 未公布 → 409
# 409 {"error":"尚未公布，不能封存"}
# 仍有"已提问未答复"的澄清 → 409
# 409 {"error":"仍有未答复的澄清问题，不能封存"}
# 仍有 pending 举报 → 409
# 409 {"error":"仍有待处理的举报，不能封存"}
# 已封存后再次封存 → 409
# 409 {"error":"会话已经封存，不能重复封存"}
# 无效主持链接 → 404（错误响应不透露会话是否存在）
curl -X POST http://localhost:8000/api/h/<无效主持token>/close
# 404 {"error":"主持链接无效"}

# 封存后：两端状态在同一快照中带 closed=true，其余只读字段照常返回
curl http://localhost:8000/api/h/<主持token>
# {"published":true,"closed":true,"can_publish":false,"reviewers":[...],"reports":[...],"links":[...]}
curl http://localhost:8000/api/p/<作者token>
# {"published":true,"closed":true,...,"received":"匿名意见原文","receipt":{...},"received_clarification":{...}}

# 封存后该会话的一切合法写请求一律 409（失败不改数据），例如：
curl -X POST http://localhost:8000/api/p/<作者token>/ack                 # 409（含重复确认已读）
curl -X POST http://localhost:8000/api/p/<作者token>/question \
  -H 'Content-Type: application/json' -d '{"question":"封存后追问"}'     # 409
curl -X POST http://localhost:8000/api/p/<评审者token>/answer \
  -H 'Content-Type: application/json' -d '{"answer":"封存后答复"}'       # 409
curl -X POST http://localhost:8000/api/p/<作者token>/report \
  -H 'Content-Type: application/json' -d '{"reason":"封存后举报"}'       # 409
curl -X POST http://localhost:8000/api/h/<主持token>/reports/12/keep     # 409（举报裁定也停用）
curl -X POST http://localhost:8000/api/h/<主持token>/rotate-link \
  -H 'Content-Type: application/json' \
  -d '{"name":"甲","link":"/p/<甲的token>"}'                            # 409（换发链接停用）
curl -X PUT http://localhost:8000/api/p/<作者token>/manuscript \
  -H 'Content-Type: application/json' -d '{"manuscript":"偷改"}'         # 409
# 统一错误体：409 {"error":"会话已封存，只读，不能再写入"}

# 封存不撤销链接：原个人链接仍可只读访问；无效/已退出/已换发作废的链接仍为 404
curl http://localhost:8000/api/p/<作者token>        # 200（只读快照，closed=true）
curl http://localhost:8000/api/p/<已作废旧token>   # 404
```

### 匿名合辑调用示例

合辑在会话公布后进行：参与者先各自授权（初始未授权，换发链接不改变授权），主持人再按姓名发布一次。公开链接任何人可读，且只含编号与稿文。

```bash
# 参与者授权本人稿件收入合辑（公布后；初始为未授权）
curl -X POST http://localhost:8000/api/p/<作者token>/anthology-consent \
  -H 'Content-Type: application/json' -d '{"consent": true}'
# 200 {"ok":true,"consent":true}
# 撤回：-d '{"consent": false}' → 200 {"ok":true,"consent":false}
# 公布前/已封存/合辑已发布 → 409；无效个人链接 → 404；consent 缺失或非布尔 → 400

# 参与状态只返回本人授权；主持状态返回各人授权与合辑公开链接（同一快照）
curl http://localhost:8000/api/p/<作者token>
# ... "anthology_consent": true ...（无 anthology_url 字段）
curl http://localhost:8000/api/h/<主持token>
# "reviewers": [{"name":"甲",...,"anthology_consent":true}, ...],
# "anthology_url": null（发布后变为 "/a/<公开token>"）

# 主持人按姓名发布匿名合辑（非空、无重复的留存名单；一次性）
curl -X POST http://localhost:8000/api/h/<主持token>/anthology \
  -H 'Content-Type: application/json' -d '{"names": ["丙", "甲"]}'
# 200 {"ok":true,"url":"/a/<公开token>"}
# 名单非法（空/重复/非字符串姓名）→ 400；主持链接无效或姓名非本会话留存成员 → 404；
# 任一入选稿未授权 → 409 {"error":"...","missing_consent":["丙"]} 且不留合辑；
# 未公布/已封存/已发布过 → 409

# 任何人读取公开合辑：只含编号与稿文，编号按原名单顺序（上例中甲为 1、丙为 2）
curl http://localhost:8000/api/a/<公开token>
# {"items":[{"number":1,"manuscript":"甲的稿文"},{"number":2,"manuscript":"丙的稿文"}]}
# 浏览器打开 http://localhost:8000/a/<公开token> 为同样的只读页面；
# 无效公开链接 → 404 {"error":"公开链接无效"}；会话封存后合辑仍可只读

# 发布后全部授权冻结：变更与重复发布一律 409
curl -X POST http://localhost:8000/api/p/<作者token>/anthology-consent \
  -H 'Content-Type: application/json' -d '{"consent": false}'          # 409
curl -X POST http://localhost:8000/api/h/<主持token>/anthology \
  -H 'Content-Type: application/json' -d '{"names": ["甲"]}'           # 409
```

### 换发个人链接调用示例

主持链接来自创建会话返回的 `/h/<host_token>`；`link` 传主持台 `GET /api/h/<host_token>` 响应 `links` 里该成员**页面所见**的个人链接（相对路径或完整 URL 均可）。公布前后都可换发。

```bash
# 主持人为「甲」换发个人链接
curl -X POST http://localhost:8000/api/h/<主持token>/rotate-link \
  -H 'Content-Type: application/json' \
  -d '{"name": "甲", "link": "/p/<甲的旧token>"}'
# 200 {"name":"甲","url":"/p/<甲的新token>"}
# 主持链接无效或姓名不存在 → 404；所见链接已过期（已被换发）/不匹配 → 409；错误响应均不含新链接

# 换发提交后：旧链接的状态读取及一切写入全部 404，且不会返回新链接
curl http://localhost:8000/api/p/<甲的旧token>                 # 404
curl -X PUT http://localhost:8000/api/p/<甲的旧token>/manuscript \
  -H 'Content-Type: application/json' -d '{"manuscript": "新稿"}'  # 404

# 新链接一切照旧：同一参与者的稿件、版本、分配、意见、问答原样保留
curl http://localhost:8000/api/p/<甲的新token>                # 200，name 仍为甲
# 主持台再次读取时，links 中甲的 url 已更新，其他人的链接与公布状态不变
curl http://localhost:8000/api/h/<主持token>
```

### 移除退出成员与分配凭据示例

仅公布前可移除，请求体只需姓名；至少保留 3 人，不能补人或恢复。

```bash
# 四人甲、乙、丙、丁（甲评乙、乙评丙、丙评丁、丁评甲）：主持人移除退出的乙
curl -X POST http://localhost:8000/api/h/<主持token>/remove \
  -H 'Content-Type: application/json' \
  -d '{"name": "乙"}'
# 200 {"ok":true}
# 主持链接无效或成员不存在（含已移除者）→ 404；已公布或移除后不足 3 人 → 409；缺姓名 → 400

# 乙的个人链接永久失效：状态读取及一切写入均 404（错误响应不透露任何其他信息）
curl http://localhost:8000/api/p/<乙的token>                       # 404
curl -X PUT http://localhost:8000/api/p/<乙的token>/manuscript \
  -H 'Content-Type: application/json' -d '{"manuscript": "新稿"}'  # 404

# 环按原录入顺序闭合为 甲→丙→丁→甲：只有"原来评乙"的甲被改分配
# 参与状态里 assignment 是甲新读到的稿件（丙的稿）与新的匿名分配凭据（不含作者姓名）
curl http://localhost:8000/api/p/<甲的token>
# { ..., "assignment": {"manuscript": "稿丙", "version": 1, "credential": "<新凭据>"},
#   "my_review": {"content": "...", "version": 1, "valid": false, "target_changed": true} }

# 甲必须带上新凭据、并绑定新稿件版本重新提交；缺凭据/沿用旧凭据 → 409（即使两篇稿版本号都是 1）
curl -X POST http://localhost:8000/api/p/<甲的token>/review \
  -H 'Content-Type: application/json' \
  -d '{"content": "甲对丙稿的意见", "version": 1, "credential": "<新凭据>"}'
# 200 {"ok":true}
curl -X POST http://localhost:8000/api/p/<甲的token>/review \
  -H 'Content-Type: application/json' \
  -d '{"content": "沿用旧凭据", "version": 1, "credential": "<旧凭据>"}'
# 409 {"error":"分配已改变，请重新阅读新分配的稿件后再提交","assignment_changed":true}

# 丙、丁的稿件、版本、意见与链接不变；他们原本的意见继续有效。
# 分配从未变过的成员仍可用原格式（不带 credential）提交：
curl -X POST http://localhost:8000/api/p/<丙的token>/review \
  -H 'Content-Type: application/json' \
  -d '{"content": "丙对丁稿的意见", "version": 1}'               # 200 {"ok":true}

# 当丙、丁与甲的新意见全部有效后，主持台 can_publish 恢复为 true，可一次公布
curl http://localhost:8000/api/h/<主持token>                      # reviewers 仅甲、丙、丁
```

### 移除首位成员示例

退出者原为当前首位时，"原来评退出者的人"是留存者中录入顺序最末的一位（末位评首位）。以甲、乙、丙、丁（稿件版本相同、均已完成评审）为例移除甲：环闭合为 乙→丙→丁→乙，只有丁改评乙；乙、丙的稿件、版本、意见、个人链接与分配凭据全部不变。

```bash
# 主持人移除退出的首位成员甲
curl -X POST http://localhost:8000/api/h/<主持token>/remove \
  -H 'Content-Type: application/json' \
  -d '{"name": "甲"}'
# 200 {"ok":true}；甲的个人链接随即永久失效（一切读取与写入 404）

# 丁（末位，原来评甲）改评乙：分配换成乙的稿件，凭据换发，旧意见已失效
curl http://localhost:8000/api/p/<丁的token>
# { ..., "assignment": {"manuscript": "稿乙", "version": 1, "credential": "<丁的新凭据>"},
#   "my_review": {"content": "...", "version": 1, "valid": false, "target_changed": true} }

# 丁缺少凭据或沿用旧凭据提交 → 409 且不写入（即使乙、甲的稿件版本号都是 1）
curl -X POST http://localhost:8000/api/p/<丁的token>/review \
  -H 'Content-Type: application/json' \
  -d '{"content": "未重读就提交", "version": 1}'
# 409 {"error":"分配已改变，请重新阅读新分配的稿件后再提交","assignment_changed":true}

# 丁持新凭据并绑定当前版本提交成功；丁补齐后主持台 can_publish 才恢复为 true
curl -X POST http://localhost:8000/api/p/<丁的token>/review \
  -H 'Content-Type: application/json' \
  -d '{"content": "丁对乙稿的意见", "version": 1, "credential": "<丁的新凭据>"}'
# 200 {"ok":true}

# 乙、丙的分配凭据未变：他们的已开页面无需确认重读，不带凭据的原格式提交仍被接受
curl -X POST http://localhost:8000/api/p/<乙的token>/review \
  -H 'Content-Type: application/json' \
  -d '{"content": "乙对丙稿的意见（改）", "version": 1}'           # 200 {"ok":true}
```

五人（甲、乙、丙、丁、戊）连续移除当前首位时规则相同：移除甲后末位的戊改评乙；再移除乙后戊改评丙——戊的凭据每次移除都重新换发，任一代旧凭据提交均 `409`。
