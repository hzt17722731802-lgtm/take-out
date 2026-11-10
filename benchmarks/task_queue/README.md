# 持久化任务队列（benchmarks/task_queue）

用 Python 标准库和 SQLite 实现的持久化任务队列，位于 `benchmarks/task_queue/`。它不依赖仓库里的 Java 服务，也不需要外部数据库或第三方包。

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `task_queue.py` | 队列实现，提供 `TaskQueue`、`Claim`、`TaskRecord` |
| `test_task_queue.py` | `unittest` 测试，覆盖接口约定和全部验收场景 |
| `README.md` | 本文件：接口、设计、运行方式与实际结果 |

## 快速开始

```python
from task_queue import TaskQueue

queue = TaskQueue("tasks.db")

task_id = queue.enqueue("order:1001", {"order_id": 1001}, max_attempts=3)

claim = queue.claim("worker-1", now=0, lease_seconds=30)
if claim is not None:
    try:
        process(claim.payload)                       # 业务处理
    except Exception:
        queue.fail(claim.task_id, claim.lease_token, now=1)   # 重新入队或永久失败
    else:
        queue.ack(claim.task_id, claim.lease_token, now=1)    # 完成

print(queue.get(task_id))   # {'id': ..., 'status': 'done', 'attempts': 1, ...}
queue.close()
```

## 接口

### `TaskQueue(db_path)`

打开（不存在时创建）`db_path` 指向的 SQLite 数据库。每个实例持有自己的连接，可以在多个线程间共享；多个实例、多个进程可以同时操作同一个数据库文件。

### `enqueue(key, payload, max_attempts=3) -> task_id`

提交任务并返回任务 ID。`key` 是幂等键：

- 同一个 `key` 提交**相同内容**时返回原任务 ID，不新建记录，也不改变原任务的状态或尝试次数；
- 同一个 `key` 提交**不同内容**时抛出 `ValueError`；
- `payload` 必须是可 JSON 序列化的值；内容相等按规范 JSON 判断（对象键顺序不影响结果）。
- `max_attempts` 是该任务最多可被领取的次数，必须为 `>= 1` 的整数，默认 3。

### `claim(worker_id, now, lease_seconds) -> Claim | None`

领取最早可执行的任务。返回本次领取的信息；没有可执行任务时返回 `None`。每次成功领取都会：

- 生成全新的随机 `lease_token`（旧的立即失效）；
- 把已领取次数加 1；
- 把任务置为 `running` 并记录 lease 到期时间 `now + lease_seconds`。

`Claim` 同时支持属性、长度 3 的元组解包和字典式访问：

```python
claim.task_id, claim.payload, claim.lease_token
task_id, payload, token = claim
claim["task_id"], claim["payload"], claim["lease_token"]
```

### `ack(task_id, lease_token, now) -> bool`

确认任务完成。仅当任务仍是 `running`、令牌是当前令牌且租约尚未过期时返回 `True`；否则返回 `False`，并且不改变任务状态。未知任务、旧令牌、过期令牌、重复确认都返回 `False`。

### `fail(task_id, lease_token, now) -> bool`

报告本次尝试失败。令牌校验规则与 `ack` 相同。校验通过时：

- 还有剩余尝试次数：任务立即重新入队（状态回到 `pending`，清空租约），可被再次领取；
- 尝试次数已用完：任务进入永久失败（`failed`），不会再被领取。

返回 `True` 表示失败已记录；无效令牌、未知任务或重复报告返回 `False` 且不改变状态。

### `get(task_id) -> TaskRecord | None`

查询任务状态，返回包含 `id`、`key`、`payload`、`status`、`attempts`、`max_attempts`、`worker_id`、`available_at`、`lease_expires_at`、`created_at`、`updated_at` 的字典（`dict` 子类，也可用 `record.status` 形式读取）。任务不存在时返回 `None`。`attempts` 即累计成功领取次数。

### 其他

- `close()` 关闭连接；`TaskQueue` 也支持 `with TaskQueue(path) as queue:`。
- 任务状态：`pending`（等待领取或失败后重新入队）、`running`（已被租约持有）、`done`（已确认完成）、`failed`（永久失败）。

## 设计要点

### 存储与状态机

```text
enqueue ──> pending ──claim──> running ──ack──> done
               ^                  │
               │                  ├──fail（还有尝试次数）──> pending
               │                  └──fail / 租约过期且次数用完 ──> failed
               └──── 租约过期且还有尝试次数，下次 claim 时重新入队
```

`tasks` 表包含任务 ID、幂等键、规范 JSON payload、状态、尝试次数、最大尝试次数、可领取时间、租约令牌、worker、租约到期时间和时间戳。`key` 上有 `UNIQUE` 约束，`seq` 自增列保证按提交顺序领取。

### 并发领取

所有写操作都通过 `BEGIN IMMEDIATE` 显式事务完成，并设置 `busy_timeout`；数据库使用 WAL 日志模式。`claim` 在事务内先处理过期租约，再按 `seq` 选出最早的可执行任务并更新，因此多个独立连接（甚至多个进程）同时争抢时，同一任务在同一时刻只会有一个有效领取者，每次成功领取恰好计为一次尝试。

### 租约与过期

- 只有 `claim`、`ack`、`fail` 接收调用方传入的 `now`，代码不依赖真实时钟判断租约；测试可以任意推进时间。
- `now` 可以是秒数（`int`/`float`）或 `datetime.datetime`。
- 租约在 `lease_expires_at <= now` 时视为过期（恰好等于到期时刻也算过期）。
- 过期的 `running` 任务在下一次 `claim` 时被处理：还有尝试次数则重新入队并可被领取；次数已用完则转为 `failed`。这是必要的惰性处理，因为 `get` 不接收 `now`，无法在查询时判断“模拟时间”是否到期。
- 重试任务没有退避等待，失败后立即可再次领取；需要退避时可在业务层延迟调用 `claim` 或另行扩展。

### 幂等

`enqueue` 用 `json.dumps(..., sort_keys=True)` 生成规范 JSON 作为内容指纹，因此对象键顺序不同但内容相同的 payload 视为同一次提交。`payload` 只支持 JSON 可表示的值，其他类型会抛出 `TypeError`。

### 持久化

任务和状态都存储在 SQLite 文件中，提交即持久化。关闭进程后重新用同一个文件创建 `TaskQueue`，仍能读到原任务、状态和尝试次数。

## 运行测试

在仓库根目录执行：

```bash
python3 -m unittest discover -s benchmarks/task_queue -p 'test_*.py' -v
```

### 实际运行结果

环境：Python 3.12.3 / Linux；起始提交 `ce3c50573decd2d94b95221ba9ba21f83a4d30ae`。

```text
$ python3 -m unittest discover -s benchmarks/task_queue -p 'test_*.py' -v
...
----------------------------------------------------------------------
Ran 26 tests in 0.738s

OK
```

26 个测试全部通过；另外连续重复运行整套测试 20 次均通过，并用 10 个独立进程同时争抢同一个任务验证过恰好 1 个进程领取成功、尝试次数为 1。

测试覆盖的验收场景：

| 验收场景 | 对应测试 |
| --- | --- |
| 同一任务提交两次只创建一条记录 | `test_same_key_and_payload_is_idempotent`、`test_same_key_different_payload_raises` |
| 10 个 worker 独立连接争抢 1 个任务，恰好 1 个成功 | `test_ten_workers_race_for_one_task` |
| A 领取后退出，租约到期后 B 可领取，A 的旧令牌无法确认 | `test_expired_lease_can_be_reclaimed_and_old_token_is_rejected` |
| 最多尝试 3 次的任务连续失败 3 次后无法再领取 | `test_task_fails_permanently_after_max_attempts` |
| 重新打开数据库后已完成任务仍为已完成 | `test_completed_task_survives_reopen` |
| 旧令牌、过期令牌、重复确认返回 `False` 且不改状态 | `test_ack_with_expired_token_does_not_change_state`、`test_repeated_ack_and_fail_return_false` 等 |
| 时间推进模拟 | 所有租约相关测试均显式传入 `now` |

## 已知边界

- 数据库路径若使用 `:memory:`，每个连接都会得到独立的空数据库；需要跨连接/进程共享时请使用文件路径。
- 各 worker 应使用同一时间基准的 `now`；队列不会把调用方的时钟与数据库服务器时间对齐（SQLite 是本地文件）。
- `claim` 返回的令牌是一次性的，`ack`/`fail` 成功或租约过期后都不能复用。
- 队列不提供任务删除、列表查询或延迟调度接口，只实现本任务要求的五个方法。
