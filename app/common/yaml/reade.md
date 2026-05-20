### 对接部署步骤
1. 后端部署 ：将 LOG_DIR 改为 K8s Pod 的日志路径（如 /home/log/cmsweb/tomcat/ ），确保 yaml 采集路径一致
2. 日志采集 ：将 agent-logs-cmsweb.yaml 部署到易监测的 K8s 集群，配置 Kafka→Doris 数据链路
3. 数据库指标 ：将 sql_exporter_cmsweb_pg_5m.yaml 部署到易监测，配置 PostgreSQL 连接信息
4. 验证 ：在 Doris 中查询日志数据，在易监测中查看 Prometheus 指标


- CMS 的 LOG_DIR = /home/log/cmsweb/tomcat （容器内路径）
- YAML 的 __path__ = {{ .Values.global.logpath }}/$1/tomcat/log_access.log
- Agent 在宿主机读 = /home/log/cmsweb-pod名/tomcat/log_access.log
情况 2：直接在容器内写日志（不挂载）

如果日志只写在容器内部，Agent 需要通过容器运行时的日志目录访问：

- CMS 的 LOG_DIR = /home/log/cmsweb/tomcat
- YAML 的 __path__ 需要改为容器内实际路径
总结：路径不重要具体是什么，重要的是 CMS 写的路径和 YAML 读的路径必须一致。 你只需要确认两件事：

1. CMS 的 LOG_DIR 环境变量指向哪里
2. YAML 中 replacement 的路径模板能否拼出相同的路径
实际部署时，把 .env 中的 LOG_DIR 和 Helm values.yaml 中的 global.logpath 协调好就行，路径本身叫什么名字无所谓， 对得上就行 。


SOLO Coder

思考过程

好问题！让我先读取 YAML 文件，然后详细解释每个配置段和路径的关系。

CMS对接平台方案说明\yaml示例\（cmsweb对接doris统一日志）agent-logs-cmsweb.yaml

CMS对接平台方案说明\yaml示例\（cmsimp容器指标对接易监测log_exporter）agent-logs-cmsimp.yaml

现在让我也看看中兴原始的 cmsweb yaml 示例，对比理解路径关系：

CMS对接平台方案说明\yaml示例\（cmsimp对接易监测sql_exporter）sql_exporter_cmsimp_pg_5m.yaml

好的，现在我可以详细解释整个 YAML 配置和路径关系了。

## YAML 配置逐段详解
### 核心概念：路径为什么重要
路径是整个采集链路的"桥梁" ，它必须让易监测的日志采集 Agent 能在 K8s Pod 的文件系统上找到 CMS 写出的日志文件。如果路径对不上，采集 Agent 就读不到日志，整个链路就断了。

```
CMS 应用写日志 → 文件系统上的路径 → 易监测 Agent 读日志 → Kafka → Doris
     ↑                    ↑
  LOG_DIR 配置        YAML __path__ 配置
  必须一致！           必须一致！
```
### YAML 整体结构
整个 YAML 由 1 个 LogsInstance + 4 个 PodLogs 组成：

```
LogsInstance (采集处理器/解析器/输出配置)
   ├── PodLogs: cmsweb-access-kafka  → 采集 log_access.log
   ├── PodLogs: cmsweb-error-kafka   → 采集 log_error.log
   ├── PodLogs: cmsweb-out-kafka     → 采集 log_out.log
   └── PodLogs: cmsweb-sql-kafka     → 采集 log_sql.log
```
### 第一部分：LogsInstance（采集处理器）
```
apiVersion: ezmon.zte.com.cn/v1alpha1
kind: LogsInstance
metadata:
  name: logsinstance-cmsweb-kafka    # 资源名称，全局唯一
  namespace: ezmon                    # 部署在 ezmon 命名空间
  labels:
    agent: zte-agent-logs             # 标识这是日志采集 Agent
spec:
  allNodes: true                      # 在所有节点上运行采集
  podLogsNamespaceSelector: { }       # 空选择器 = 扫描所有命名空间
  podLogsSelector:
    matchLabels:
      instance: primary               # 匹配 primary 实例
      devicetype: cmsweb-kafka        # 匹配 cmsweb-kafka 类型的 PodLogs
  externalLabels:
    devicetype: "cmsweb"              # 附加到所有日志的外部标签
```
关键配置：bloblang 处理器

```
  processes:
    threads: 3                        # 3 个并发处理线程
    processors:
      - bloblang: |                   # 第一步：解析日志行
          # 将 "key=value||key=value" 格式解析为 JSON 对象
          root = this.line.split("||").map_each(pair -> { 
            pair.split("=").0: pair.split("=").1 
          }).fold({}, item -> item.tally.merge(item.value))
          
          # 附加 K8s 元数据标签
          root.devicetype = this.labels.devicetype      # 设备类型
          root.deviceip = this.labels.host_ip           # Pod 所在节点 IP
          root.devicename = this.labels.pod_name        # Pod 名称
          root.deviceid = this.labels.deviceid          # 设备 ID
          root.filename = this.labels.filename           # 日志文件名
          root.sourcelog = this.line                     # 原始日志行（保留）
          root.platformid = this.labels.platform         # 平台标识
          root.time = now().format_timestamp_strftime("%Y-%m-%d %H:%M:%S.%f")
          root.p_hour = now().format_timestamp_strftime("%Y-%m-%d %H:00:00")  # 按
          小时分区

      - bloblang: |                   # 第二步：拆分文件路径
          root.filepath = this.filename.split("/").slice(0, -1).join("/")  # 目录路
          径
          root.filename = this.filename.split("/").slice(-1).0              # 文件
          名
          # ... 其他字段透传
```
这段是核心 ：它把 CMS 输出的 time=xxx||level=xxx||servicetype=xxx||... 格式解析成结构化 JSON，然后推送到 Kafka→Doris。

输出配置 ：

```
  output:
    fallback:
      {{- include "kafka.config.iptv-other-log" . | nindent 6 }}  # 主输出：Kafka
      - drop: {}                    # 降级：丢弃（Kafka 不可用时）
```
### 第二部分：PodLogs（日志文件发现与路径配置）
每个 PodLogs 定义了 如何找到 Pod 中的日志文件 。以 access 日志为例：

```
apiVersion: ezmon.zte.com.cn/v1alpha1
kind: PodLogs
metadata:
  labels:
    instance: primary                # 必须与 LogsInstance 的 podLogsSelector 匹配
    devicetype: cmsweb-kafka         # 必须与 LogsInstance 的 podLogsSelector 匹配
    logtype: cmsweb-access-kafka     # 日志类型标识
  name: cmsweb-access-kafka          # 资源名称
  namespace: ezmon
spec:
  jobLabel: "cmsweb-access-kafka"    # 采集任务标签
  namespaceSelector:
    matchNames:
      - {{ .Values.global.bvsNamespace }}  # CMS Pod 所在的命名空间
  selector:
    matchLabels:
      devicetype: cmsweb             # 匹配 Pod 上的标签 devicetype=cmsweb
```
最关键的 relabelings（路径映射） ：

```
  relabelings:
    # 1. 只保留 devicetype=cmsweb 的 Pod
    - sourceLabels: [ __meta_kubernetes_pod_label_devicetype ]
      regex: cmsweb
      action: keep

    # 2. 记录 Pod 所在节点名称
    - sourceLabels: [ __meta_kubernetes_pod_node_name ]
      targetLabel: __host__

    # 3. 提取 Pod 名称
    - sourceLabels: [__meta_kubernetes_pod_name]
      action: replace
      targetLabel: pod_name

    # ★★★ 4. 构建日志文件路径（最关键！） ★★★
    - sourceLabels: [ pod_name ]
      targetLabel: __path__
      replacement: {{ .Values.global.logpath }}/$1/tomcat/log_access.log
      #                  ↑                        ↑       ↑
      #                  全局日志根路径              Pod名   子目录+文件名
      #                  (Helm values 配置)        ($1)    
      #
      # 最终路径示例：/home/log/cmsweb-pod-abc/tomcat/log_access.log
      #                   ↑              ↑        ↑       ↑
      #                   global.logpath  Pod名   tomcat/  日志文件名
```
### 路径对应关系图
```
┌─────────────────────────────────────────────────────────────┐
│                    K8s Pod 文件系统                          │
│                                                             │
│  /home/log/                    ← global.logpath (Helm配置)  │
│  └── cmsweb-pod-abc/           ← Pod 名称 ($1)             │
│      └── tomcat/               ← 子目录                     │
│          ├── log_access.log    ← CMS 写出的 access 日志     │
│          ├── log_error.log     ← CMS 写出的 error 日志      │
│          ├── log_out.log       ← CMS 写出的 out 日志        │
│          └── log_sql.log       ← CMS 写出的 sql 日志        │
│                                                             │
└─────────────────────────────────────────────────────────────┘
         ↑ 写入                          ↑ 读取
    CMS 应用配置                    易监测 Agent 采集
    LOG_DIR=                       __path__=
    /home/log/cmsweb/tomcat        {{.Values.global.logpath}}/$1/tomcat/log_xxx.log
```
路径必须完全一致！ CMS 的 LOG_DIR 写到哪里，YAML 的 __path__ 就必须指向哪里。

### 路径中各部分的含义
路径部分 来源 说明 {{ .Values.global.logpath }} Helm values.yaml 全局配置 日志根路径，中兴环境通常为 /home/log $1 Pod 名称自动替换 从 __meta_kubernetes_pod_name 获取，如 cmsweb-7d8f9c6b5-x2k4j tomcat/ 硬编码子目录 中兴约定俗成的子目录名（历史原因，虽然不是 Java 但沿用了 tomcat 目录名） log_access.log 硬编码文件名 必须与 CMS logging_config.py 中的文件名完全一致

### 对比中兴原始 cmsimp 的路径
```
# cmsimp 的路径配置（中兴原始示例）
replacement: /home/log/$1/zxcom.log
#              ↑        ↑    ↑
#              根路径    Pod名  日志文件名（无 tomcat 子目录）
```
```
# cmsweb 的路径配置（我们的 YAML）
replacement: {{ .Values.global.logpath }}/$1/tomcat/log_access.log
#              ↑                          ↑    ↑       ↑
#              Helm变量                    Pod名  子目录   文件名
```
区别在于 cmsweb 多了一个 tomcat/ 子目录，这是中兴那边对 Web 类服务的约定。

### 实际部署时需要改什么
情况 1：CMS Pod 的日志挂载到宿主机

如果 K8s Deployment 中配置了 hostPath 或 PVC 挂载：

```
# K8s Deployment volume 配置
volumes:
  - hostPath:
      path: /home/log/cmsweb    # 宿主机路径
    name: log-volume
volumeMounts:
  - mountPath: /home/log/cmsweb/tomcat   # 容器内路径
    name: log-volume
```
那么：

- CMS 的 LOG_DIR = /home/log/cmsweb/tomcat （容器内路径）
- YAML 的 __path__ = {{ .Values.global.logpath }}/$1/tomcat/log_access.log
- Agent 在宿主机读 = /home/log/cmsweb-pod名/tomcat/log_access.log
情况 2：直接在容器内写日志（不挂载）

如果日志只写在容器内部，Agent 需要通过容器运行时的日志目录访问：

- CMS 的 LOG_DIR = /home/log/cmsweb/tomcat
- YAML 的 __path__ 需要改为容器内实际路径
总结：路径不重要具体是什么，重要的是 CMS 写的路径和 YAML 读的路径必须一致。 你只需要确认两件事：

1. CMS 的 LOG_DIR 环境变量指向哪里
2. YAML 中 replacement 的路径模板能否拼出相同的路径
实际部署时，把 .env 中的 LOG_DIR 和 Helm values.yaml 中的 global.logpath 协调好就行，路径本身叫什么名字无所谓， 对得上就行 。

09:40
hzh

- CMS 的 LOG_DIR = /home/log/cmsweb/tomcat （容器内路径）

- YAML 的 __path__ = {{ .Values.global.logpath }}/$1/tomcat/log_access.log  我也没有发现他们怎么是一致的啊，

SOLO Coder

思考过程

非常好的问题！你说得对，这两个路径看起来确实不一致。让我画清楚它们之间的关系。

## 核心问题：容器内路径 vs 宿主机路径
CMS 的 LOG_DIR 是 容器内路径 ，YAML 的 __path__ 是 宿主机路径 。它们不是同一个路径，而是通过 K8s Volume 挂载 连接起来的。

```
┌──────────────────────────────────────────────────────────────┐
│  K8s Node (宿主机)                                           │
│                                                              │
│  /home/log/                          ← global.logpath       │
│  ├── cmsweb-7d8f9c6b5-x2k4j/        ← $1 (Pod名)           │
│  │   └── tomcat/                                              │
│  │       ├── log_access.log          ← Agent 读这个文件      │
│  │       ├── log_error.log                                    │
│  │       ├── log_out.log                                      │
│  │       └── log_sql.log                                      │
│  ├── cmsweb-7d8f9c6b5-m3n7p/        ← 另一个 Pod           │
│  │   └── tomcat/                                              │
│  │       └── ...                                              │
│  └── cmsimp-5c4b3a2d1-q8w9e/                                │
│      └── zxcom.log                                            │
│                                                              │
│  ┌────────────────────────┐  ┌────────────────────────────┐  │
│  │  CMS Pod               │  │  易监测 Agent (DaemonSet)   │  │
│  │                        │  │                            │  │
│  │  容器内:               │  │  宿主机:                   │  │
│  │  /home/log/cmsweb/     │  │  /home/log/               │  │
│  │    tomcat/             │  │    cmsweb-7d8f.../         │  │
│  │      log_access.log ◄─┼──┼──►log_access.log           │  │
│  │      log_error.log    │  │      log_error.log          │  │
│  │                        │  │                            │  │
│  │  LOG_DIR=              │  │  __path__=                 │  │
│  │  /home/log/cmsweb/     │  │  /home/log/$1/tomcat/      │  │
│  │    tomcat              │  │    log_access.log          │  │
│  └────────────────────────┘  └────────────────────────────┘  │
│         ↑                              ↑                     │
│     VolumeMount                    读取宿主机路径             │
│     (挂载映射)                     (不进容器)                 │
└──────────────────────────────────────────────────────────────┘
```
## 连接桥梁：K8s Volume 挂载
CMS Deployment 中需要配置 volume 挂载，把容器内的路径映射到宿主机：

```
# CMS K8s Deployment 配置
spec:
  containers:
    - name: cmsweb
      env:
        - name: LOG_DIR
          value: "/home/log/cmsweb/tomcat"     # 容器内路径
        - name: POD_NAME                        # 获取 Pod 名称
          valueFrom:
            fieldRef:
              fieldPath: metadata.name
      volumeMounts:
        - name: log-volume
          mountPath: /home/log/cmsweb/tomcat    # 容器内挂载点
          subPathExpr: $(POD_NAME)/tomcat       # 宿主机子路径 = Pod名/tomcat
  volumes:
    - name: log-volume
      hostPath:
        path: /home/log                         # 宿主机根路径
        type: DirectoryOrCreate
```
挂载效果 ：

容器内路径 宿主机路径 /home/log/cmsweb/tomcat/log_access.log /home/log/cmsweb-7d8f9c6b5-x2k4j/tomcat/log_access.log /home/log/cmsweb/tomcat/log_error.log /home/log/cmsweb-7d8f9c6b5-x2k4j/tomcat/log_error.log

路径对应关系 ：

```
CMS 写入（容器内）:  /home/log/cmsweb/tomcat/log_access.log
                       ─────────  ──────  ──────  ─────────────
                       hostPath   subPathExpr  固定    文件名
                       (宿主机根)  (Pod名/tomcat)

Agent 读取（宿主机）: /home/log/cmsweb-7d8f9c6b5-x2k4j/tomcat/log_access.log
                       ─────────  ─────────────────────  ──────  ─────────────
                       global.logpath    $1 (Pod名)      tomcat/   文件名
```
## 所以路径为什么"看起来不一致"
因为它们本来就是 两个不同视角 的路径：

- CMS 视角 （容器内）： /home/log/cmsweb/tomcat/ — 固定路径，所有 Pod 都一样
- Agent 视角 （宿主机）： /home/log/<pod名>/tomcat/ — 按 Pod 名区分，每个 Pod 不同
cmsweb ≠ cmsweb-7d8f9c6b5-x2k4j ，但通过 K8s 的 subPathExpr: $(POD_NAME)/tomcat 挂载，容器内的 /home/log/cmsweb/tomcat/ 实际上映射到了宿主机的 /home/log/cmsweb-7d8f9c6b5-x2k4j/tomcat/ 。

## 实际部署时你需要做什么
CMS LOG_DIR /home/log/cmsweb/tomcat .env 或 K8s env 2 K8s hostPath.path /home/log Deployment volumes 3 Helm global.logpath /home/log values.yaml

只要 hostPath.path = global.logpath （都是 /home/log ），挂载关系就对了。CMS 容器内看到的路径和 Agent 在宿主机看到的路径，通过 volume mount 自动对应。