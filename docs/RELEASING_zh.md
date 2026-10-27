# PhyAgentOS GitHub 与 PyPI 发布流程

[English](RELEASING.md) | [简体中文](RELEASING_zh.md)

`release-build.yml` 负责构建并测试 tag。`publish-pypi.yml` 将 **GitHub Release 中已有的
wheel 与源码包**上传到 PyPI 的 `PhyAgentOS-ai` 项目，再核对哈希并在全新的 Python 3.12
环境中从官方索引安装。发布阶段不重新构建，也不向 PyPI 上传 `SHA256SUMS`。

## 一次性认证配置

在仓库 Actions secrets 中配置 `PYPI_API_TOKEN`，使用有权发布 `PhyAgentOS-ai` 的 PyPI API
token，优先使用仅限该项目的 token。`pypi` environment 标识正式发布，token 仅在上传 job
使用。不要将 token 提交到仓库或填入工作流输入参数。

## 发布新版本

1. 同步更新 Python 包、运行时、bridge 的版本号，以及 README 徽章、发布亮点、News、
   changelog 和带版本的安装示例。审核并将发布改动从 `dev` 合并到 `main`。
2. 在确定的发布提交上创建 **annotated tag**，格式为 `v主版本.次版本.补丁版本`，annotation
   延续上一版本的格式。tag 对应代码树必须包含发布工作流；不要移动已发布的 tag。
3. 等待 **Build release package** 成功，下载其 `phyagentos-v主版本.次版本.补丁版本`
   artifact，包含 wheel、源码包和 `SHA256SUMS`。
4. 为该 tag 创建 GitHub **草稿 Release**，原样附加上述三个文件，完成发布说明后再发布为
   正式版本。务必在点击 Publish release **之前**上传附件。
5. `release: published` 事件会自动启动 **Publish release to PyPI**。确认 `prepare`、
   `publish`、`verify` 三个 job 均成功后，再宣布 PyPI 可用。草稿与预发布版本不触发上传。

tag、构建和发布说明仍由维护者确认，PyPI 上传与验证自动完成。如果后续用另一个 workflow
的 `GITHUB_TOKEN` 创建 Release，该事件**不会**再触发发布工作流；需要在附件上传完成后
显式 dispatch `publish-pypi.yml`，或使用获得相应授权的 GitHub App/PAT 发布 Release。

## 重试与补发

进入 Actions → **Publish release to PyPI** → **Run workflow**，选择 `main`，输入已有的
正式 Release tag。也可以运行：

```sh
gh workflow run publish-pypi.yml \
  --repo PhyAgentOS/PhyAgentOS-core --ref main -f tag=v1.1.1
```

工作流要求 GitHub Release 的三个附件齐全，核对 SHA256、包内项目名称和版本，再查询 PyPI：

- 文件已存在且哈希一致：跳过上传，仍检查线上哈希并重新安装验证。
- 仅部分上传成功：重试时只上传缺失的文件。
- 同名文件内容冲突、文件被 yank、出现非预期文件或元数据不符：明确失败。修复输入或发布
  新版本，不覆盖已有 PyPI 版本。
- 凭据缺失或 PyPI 服务异常：运行明确失败；修复原因后重试，同一份制品无需提高版本号。

对已发布版本的重复运行可以验证流程及安装，但不代表完成了一次新的认证上传。

## 后续切换到 Trusted Publishing

本次接入使用已有 API token。若后续希望取消长期 secret，可在 PyPI 项目的 **Publishing**
设置中添加 GitHub Trusted Publisher，准确填写：

| 字段 | 值 |
| --- | --- |
| Owner | `PhyAgentOS` |
| Repository | `PhyAgentOS-core` |
| Workflow filename | `publish-pypi.yml` |
| Environment | `pypi` |

然后只调整 `publish` job：添加 `permissions: {id-token: write, contents: read}`，移除凭据
检查步骤及 action 的 `password` 参数，并移除 `attestations: false` 以启用证明文件。完成
一次认证发布后，再删除 GitHub secret 并在 PyPI 撤销旧 token。不要先删除 secret 再配置
publisher 和修改工作流。

参考：[PyPA 发布 Action](https://github.com/pypa/gh-action-pypi-publish)、
[PyPI Trusted Publisher 配置](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)、
[GitHub 工作流触发规则](https://docs.github.com/en/actions/how-tos/writing-workflows/choosing-when-your-workflow-runs/triggering-a-workflow#triggering-a-workflow-from-a-workflow)。
