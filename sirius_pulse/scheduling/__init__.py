"""她名下所有「到点要做的事」的统一调度：一张到期任务表、一个 ticker、一个 cron 匹配器。

提醒、自我唤醒、bash 定时任务、转后台的 bash 命令、生图队列、视觉自动候选、
生活心跳都登记在 ``persona.db`` 的 ``due_jobs`` 表里，由每个人格一个的
:class:`~sirius_pulse.scheduling.due_jobs.DueJobTicker` 在最近的到期时刻醒来处理；
新增任务会立刻唤醒它，不再是七个各自按间隔轮询的循环。

这里既不依赖 ``core`` 也不依赖 ``tools``：工具和引擎都可以用它。
"""
