"""总后台（Console）业务逻辑包

`instance_manager` 负责实例的生命周期与统计聚合；容器托管（`container_manager`）与
掉线监测由并行开发的其他模块补齐，本包不对它们做硬依赖。
"""
