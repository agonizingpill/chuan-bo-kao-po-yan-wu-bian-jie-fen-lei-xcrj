# 初始 schema

`src.models.Base.metadata.create_all()` 按外键顺序建表，由 `src.main` 在 API 首次启动时执行。已有开发卷不执行自动字段迁移。项目 README 记录了模型与首次建库入口。

## 待命整批（`standby_batches` / `standby_plan_items` / `standby_tug_holds`）

这三张表由 `create_all` 一并建立，用于「几艘船争用待命拖轮」的整批预排与发布：

- `standby_batches` 保存批次状态（`planned` / `published` / `returned` / `superseded`）、说明、退回原因与发布人。
- `standby_plan_items` 每批每案一行，记录排序名次、预排拖轮与时段；排不上的案件写 `blocked` 和原因。`(batch_code, incident_code)` 唯一。
- `standby_tug_holds` 只在整批确认成功后写入，表示已发布的待命占用，`released_at` 为空时参与拖轮冲突校验。

已有开发卷如需使用待命整批，需要重建数据库卷或手工建表；本项目不执行自动字段迁移。
