# Piper机械臂快速启动

按顺序完成环境配置、Skill 安装、CAN 接口激活和真机启动。每条命令下方列出了应检查的状态与注意事项。

## 0. 安装并配置 PAOS

先完整按照 PhyAgentOS-core 官方 Quickstart 的操作。

[打开官方 Quickstart](../../README_zh.md#quick-start)

**完成标准：PAOS 环境安装完成，能够通过 paos agent 进行对话。**

## 1. 安装 Skill

在安装 PAOS 的环境（例如同一个 conda 环境）中执行。

### 安装 move-arm-by-ee Skill

```bash
paos skill install move-arm-by-ee
```

**检查结果：** 安装完成后，可用下一条命令查询 Skill 的版本和 profile。

### 查看版本与可用 profile

```bash
paos skill inspect move-arm-by-ee
```

**检查结果：** 应显示版本及全部 profile，其中包括 piper_real。

### 确认安装后的初始状态

```bash
paos skill status move-arm-by-ee
```

**检查结果：** State 显示 not started 属正常；此时还没有连接真机。

## 2. 连接硬件并激活 CAN

先给机械臂上电，接好电脑与机械臂之间的 USB-CAN 通信链路。下面以接口 can0 为例。

### 列出 CAN 接口，先确认实际设备名

```bash
ip -details link show type can
```

**检查结果：** 应能找到 CAN 接口及其名称，例如 can0；激活前 state DOWN 属正常。

**注意：** 若没有接口，请先检查 USB-CAN 转接器和连接。

### 设置波特率；此命令只配置，不激活接口

```bash
sudo ip link set can0 type can bitrate 1000000
```

**检查结果：** 配置完成后继续执行激活命令，并用最后的状态查询确认结果。

**注意：** 如果设备名不是 can0，请替换下列命令中的 can0。

### 激活 CAN 接口

```bash
sudo ip link set can0 up
```

**检查结果：** 接口应从 DOWN 变为 UP；下一条命令会显示实际状态。

### 检查 CAN 接口状态

```bash
ip -details link show can0
```

**检查结果：** 应看到 state UP。若仍为 DOWN，请先处理接口问题再启动 Skill。

## 3. 启动 Piper 真机 Profile

piper_real 指定 Piper 真机 runtime profile；其他 profile 可在第 1 步的 inspect 结果中查看。

### 启动并连接真机

```bash
paos skill start move-arm-by-ee -p piper_real
```

**检查结果：** 驱动连接后，机械臂会自动移动到初始位姿；随后用下一步的 status 检查 Tool 就绪状态。

**注意：** 启动前清理机械臂周围的障碍物，确认急停可用；Dora CLI 需已按官方说明安装并位于 PATH 中。

## 4. 验证工具就绪

不要仅凭启动命令返回判断真机可以操作，先检查 Runtime 与 Tool 状态。

### 查询 Skill 与 Gateway 状态

```bash
paos skill status move-arm-by-ee
```

**检查结果：** 应看到 State: running、Gateway GET /tools: ready，且 motion.resolve_relative_pose、motion.move_pose、gripper.set_opening 三个 Tool 均为 ready。

## 5. 启动 Agent 对话

Skill 处于运行状态时，Agent 才会注入机械臂工具。下面两种方式任选其一。

### 进入交互模式，可输入“把机械臂抬升 3 厘米”等指令

```bash
paos agent
```

**检查结果：** 进入 Agent 对话；可以继续输入“机械臂前伸 3 厘米”或“把夹爪打开 5 厘米”等请求。

### 或者只发送一条消息

```bash
paos agent -m "把机械臂抬升 3 厘米"
```

**检查结果：** 发送一条抬升机械臂的请求；这是交互模式的替代方式，无须两条命令都运行。

## 6. 结束并关闭 Skill

完成真机操作后停止 Skill。

### 停止 move-arm-by-ee Skill

```bash
paos skill stop move-arm-by-ee
```

**检查结果：** 断开前机械臂会回到初始位姿。

**注意：** 停止过程中也要保持机械臂周围无障碍。
