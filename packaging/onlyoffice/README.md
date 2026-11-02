# 本机 ONLYOFFICE 服务

`start-local.sh` 是 Linux 服务主机管理员的准备脚本，不需要业务用户安装运行环境；它将服务放入 Docker `--internal` 私有网络，通过本机 `127.0.0.1:18080` 转发，首次启动生成随机 JWT 密钥，数据保存在 Docker 命名卷中。脚本每次启动动态读取容器地址并重建临时 systemd 转发单元。运行前需要 Docker、systemd、socat、OpenSSL、足够磁盘空间及镜像下载网络，通常以管理员身份运行。默认固定 ONLYOFFICE 10.0 的官方镜像索引摘要 `sha256:ca17889f10b11141cc6441836aebdd2835cf3e1b49681b2fd6e155c94b7850b3`；可以使用 `ATLAS_ONLYOFFICE_IMAGE` 指定管理员已验证来源和摘要的镜像。

停止服务：`systemctl stop atlas-onlyoffice-local-proxy.service`，再执行 `docker stop atlas-onlyoffice`。重启机器后需再次运行启动脚本以重新建立转发。内网隔离不是数据权限管理，也不代表产品接入已完成。

服务健康不代表已经与 Atlas 集成。产品接入还必须完成：按项目和用户授权签发短期文件访问、配置签名 JWT、回调验证和版本保存、限制文档服务下载地址和网络访问、处理大文件和过期链接，并在真实服务上测试。不能将项目口令、永久文件地址或客户文件交给公共预览站点。未完成以上工作前不能对用户宣称可以在线编辑。

社区版文档：https://helpcenter.onlyoffice.com/docs/installation/docs-community-install-docker.aspx
