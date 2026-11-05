# 苍穹外卖（sky-take-out）

苍穹外卖是一个基于 Spring Boot 的外卖订餐系统后端，采用 Maven 多模块结构，按管理端（`/admin`）和用户端（`/user`）划分 HTTP 接口。

当前仓库仍在开发中：**已实现**员工管理、分类管理、菜品新增、店铺营业状态和文件上传等功能；套餐、订单、支付、购物车、用户登录、数据统计等只有实体、DTO/VO、依赖或工具类，业务接口尚未完成。功能状态以本文档的“功能状态”一节为准。

> 本文档根据当前仓库源码整理。仓库中**没有**运行配置文件、数据库 SQL 和前端工程，构建与启动步骤**未在真实环境中执行验证**，具体限制见“数据库与缺失材料”和“验证状态与限制”。

## 技术栈

| 分类 | 组件 | 版本 / 说明 |
| --- | --- | --- |
| 基础框架 | Spring Boot | 2.7.3（父 POM） |
| Web | Spring MVC | `spring-boot-starter-web` |
| 持久层 | MyBatis | 2.2.0（`mybatis-spring-boot-starter`） |
| 分页 | PageHelper | 1.3.0 |
| 数据库 | MySQL Connector/J | 版本由 Spring Boot 管理 |
| 连接池 | Druid | 1.2.1 |
| 缓存 | Redis | `spring-boot-starter-data-redis`，自定义 `RedisTemplate` |
| 认证 | JJWT | 0.9.1（HS256） |
| 接口文档 | Knife4j | 3.0.2（基于 Springfox / Swagger 2） |
| 对象映射 | Lombok | 1.18.20 |
| JSON | Jackson、Fastjson | Jackson 自定义 `JacksonObjectMapper`；Fastjson 1.2.76 |
| AOP | AspectJ | 1.9.4 |
| 文件存储 | 阿里云 OSS SDK | 3.10.2 |
| 微信支付 | wechatpay-apache-httpclient | 0.4.8（仅引入依赖，未实现支付业务） |
| 报表导出 | Apache POI | 3.16（仅引入依赖，未实现导出） |
| 构建 | Maven | 多模块，未提供 Maven Wrapper |

## 功能状态

### 已实现

| 功能 | 说明 |
| --- | --- |
| 员工登录 / 退出 | 登录成功后签发 JWT；退出接口仅返回成功，服务端不维护令牌状态 |
| 员工新增 / 分页 / 详情 / 编辑 / 启用禁用 | 新增员工默认密码为 `123456`（MD5 存储），默认启用；详情接口对密码脱敏 |
| 分类新增 / 分页 / 删除 / 修改 / 启用禁用 / 按类型查询 | 删除分类前会校验是否已关联菜品或套餐 |
| 新增菜品（含口味） | 菜品与口味在同一事务中写入 |
| 文件上传 | 上传到阿里云 OSS，需要 OSS 配置 |
| 店铺营业状态 | 状态保存在 Redis 键 `SHOP_STATUS` 中；管理端可设置和查询，用户端只读 |
| 通用基础设施 | 管理端 JWT 校验、公共字段自动填充 AOP、全局异常处理、统一返回结果、PageHelper 分页、Knife4j 接口文档、Java 8 时间类型 JSON 序列化 |

### 尚未实现

以下功能**不能**视为已完成，仓库中只有数据模型、依赖或辅助代码：

| 功能 | 仓库现状 |
| --- | --- |
| 套餐管理 | 只有 `SetmealMapper` 的关联数量查询，以及 DTO、VO、实体，无 Controller/Service |
| 菜品查询、修改、起售、停售、删除 | 只有“新增菜品”接口 |
| 订单管理与支付 | 只有实体、DTO/VO、微信支付工具类和依赖，无业务接口 |
| 购物车 | 只有实体、DTO 和业务异常类 |
| 用户登录、地址簿 | 只有数据模型和异常类 |
| 营业额、订单统计、报表导出 | 只有 DTO/VO 和 POI 依赖，无统计或导出代码 |
| 用户端业务 | 只有 `/user/shop/status` 一个接口，且没有用户端登录鉴权 |
| WebSocket 相关功能 | 只有 `spring-boot-starter-websocket` 依赖 |

### 已暴露的接口

| 方法 | 路径 | 说明 | 管理端 JWT |
| --- | --- | --- | --- |
| POST | `/admin/employee/login` | 员工登录 | 否 |
| POST | `/admin/employee/logout` | 员工退出（仅返回成功） | 是 |
| POST | `/admin/employee` | 新增员工 | 是 |
| GET | `/admin/employee/page` | 员工分页查询（可按 `name` 过滤） | 是 |
| POST | `/admin/employee/status/{status}?id=` | 启用或禁用员工 | 是 |
| GET | `/admin/employee/{id}` | 查询员工详情 | 是 |
| PUT | `/admin/employee` | 编辑员工 | 是 |
| POST | `/admin/category` | 新增分类 | 是 |
| GET | `/admin/category/page` | 分类分页查询（可按 `name`、`type` 过滤） | 是 |
| DELETE | `/admin/category?id=` | 删除分类 | 是 |
| PUT | `/admin/category` | 修改分类 | 是 |
| POST | `/admin/category/status/{status}?id=` | 启用或禁用分类 | 是 |
| GET | `/admin/category/list?type=` | 按类型查询启用中的分类 | 是 |
| POST | `/admin/dish` | 新增菜品及口味 | 是 |
| POST | `/admin/common/upload` | 上传文件到 OSS | 是 |
| PUT | `/admin/shop/{status}` | 设置营业状态（`1` 营业，`0` 打烊） | 是 |
| GET | `/admin/shop/status` | 查询营业状态 | 是 |
| GET | `/user/shop/status` | 用户端查询营业状态 | 否 |

管理端 JWT 校验由 [JwtTokenAdminInterceptor.java](sky-server/src/main/java/com/sky/interceptor/JwtTokenAdminInterceptor.java) 实现，只拦截 `/admin/**`（登录接口除外），令牌放在配置的请求头中。用户端接口目前没有拦截器。

## 项目结构

```text
take-out/
├── pom.xml                         # 父 POM，聚合三个模块并统一依赖版本
├── sky-common/                     # 公共模块：常量、异常、工具、配置属性
│   └── src/main/java/com/sky/
│       ├── constant/               # 常量（状态、默认密码、消息等）
│       ├── context/                # BaseContext，ThreadLocal 保存当前用户 ID
│       ├── enumeration/            # AutoFill 操作类型（INSERT/UPDATE）
│       ├── exception/              # 业务异常与 BaseException
│       ├── json/                   # JacksonObjectMapper
│       ├── properties/             # sky.jwt / sky.alioss / sky.wechat 配置属性
│       ├── result/                 # Result、PageResult
│       └── utils/                  # JWT、OSS、HTTP、微信支付工具类
├── sky-pojo/                       # 数据模型模块，无业务逻辑
│   └── src/main/java/com/sky/
│       ├── dto/                    # 请求参数对象
│       ├── entity/                 # 数据库实体
│       └── vo/                     # 响应视图对象
└── sky-server/                     # 启动与业务模块
    ├── pom.xml
    └── src/main/
        ├── java/com/sky/
        │   ├── SkyApplication.java # 启动类
        │   ├── annonation/         # @AutoFill 注解（包名拼写为 annonation）
        │   ├── aspect/             # 公共字段自动填充切面
        │   ├── config/             # Web MVC、Redis、OSS 配置
        │   ├── controller/admin/   # 管理端接口
        │   ├── controller/user/    # 用户端接口（目前只有店铺状态）
        │   ├── handler/            # 全局异常处理
        │   ├── interceptor/        # 管理端 JWT 拦截器
        │   ├── mapper/             # MyBatis Mapper 接口
        │   └── service/            # 业务接口及 impl 实现
        └── resources/
            └── mapper/             # MyBatis XML 映射文件
```

三个模块的职责：

| 模块 | 职责 | 依赖关系 |
| --- | --- | --- |
| [sky-common](sky-common/pom.xml) | 跨模块复用的常量、异常、返回结果、工具类和配置属性 | 不依赖其他业务模块 |
| [sky-pojo](sky-pojo/pom.xml) | DTO、实体和 VO，只承载数据 | 不依赖其他业务模块 |
| [sky-server](sky-server/pom.xml) | 启动类、Web 层、业务层、持久层和 MyBatis 映射 | 依赖 `sky-common` 和 `sky-pojo` |

入口类为 [SkyApplication.java](sky-server/src/main/java/com/sky/SkyApplication.java)，根 POM 见 [pom.xml](pom.xml)。

## 环境要求

- **JDK 8**：项目未显式声明 `java.version`，Spring Boot 2.7.3 父 POM 默认按 Java 8 编译。
- **Maven 3.6+**：仓库没有 `mvnw`，需要本机安装 Maven。
- **MySQL**：用于业务数据；连接器版本由 Spring Boot 管理，仓库未固定数据库版本，也未提供建表脚本。
- **Redis**：用于保存店铺营业状态，未在仓库中固定版本。
- **阿里云 OSS**：仅在调用文件上传接口时需要，需准备 Endpoint、AccessKey 和 Bucket。
- **网络**：首次构建需要下载 Maven 依赖；文件上传需要访问 OSS。

微信支付和 WebSocket 虽然引入了依赖，但当前没有对应业务代码，不需要配置即可启动。

## 构建

在项目根目录执行：

```bash
mvn clean package -DskipTests
```

也可以只构建服务模块及其依赖：

```bash
mvn -pl sky-server -am clean package -DskipTests
```

构建产物为 `sky-server/target/sky-server-1.0-SNAPSHOT.jar`。

> 上述构建命令未在本次提交的环境中实际执行验证，仅根据 POM 配置整理。

## 配置

仓库中**没有** `application.yml`，且 [.gitignore](.gitignore) 忽略了 `sky-server/src/main/resources/application*.yml`。因此需要在本机创建配置文件，不能直接依赖仓库启动。

可参考下面的最小示例，尖括号内容均为占位符，请替换为实际值，**不要提交真实凭据**：

```yaml
server:
  port: 8080

spring:
  datasource:
    druid:
      driver-class-name: com.mysql.cj.jdbc.Driver
      url: "jdbc:mysql://127.0.0.1:3306/<数据库名>?serverTimezone=Asia/Shanghai&useUnicode=true&characterEncoding=utf-8&useSSL=false&allowPublicKeyRetrieval=true"
      username: <数据库用户名>
      password: <数据库密码>
  redis:
    host: 127.0.0.1
    port: 6379
    password: <Redis 密码>   # 无密码时删除本行或留空
    database: 0

mybatis:
  mapper-locations: classpath:mapper/*.xml
  type-aliases-package: com.sky.entity
  configuration:
    map-underscore-to-camel-case: true

sky:
  jwt:
    admin-secret-key: <自定义管理端 JWT 密钥>
    admin-ttl: 7200000
    admin-token-name: token
  alioss:
    endpoint: <OSS Endpoint，例如 oss-cn-hangzhou.aliyuncs.com>
    access-key-id: <AccessKeyId>
    access-key-secret: <AccessKeySecret>
    bucket-name: <Bucket 名称>

logging:
  level:
    com.sky.mapper: debug
```

配置说明：

- `sky.jwt.admin-*` 是管理端登录和 JWT 校验所需配置，`admin-token-name` 决定请求头名称。
- `sky.alioss.*` 是文件上传所需配置；不调用上传接口时可以先不填。
- `sky.jwt.user-*` 和 `sky.wechat.*` 在配置属性类中已定义，但当前代码没有使用，可以不配置。
- `map-underscore-to-camel-case` 需要开启，否则 `create_time` 等下划线字段无法映射到实体中的驼峰属性。
- 这些配置项依据属性类和源码整理，未实际启动验证。

## 运行

1. 启动 MySQL 和 Redis。
2. 创建数据库及所需数据表（仓库未提供 SQL，见下一节）。
3. 在 `sky-server/src/main/resources/` 下创建 `application.yml`，写入上一节的配置。
4. 启动应用，任选一种方式：

   ```bash
   # 已执行 mvn clean package 后
   java -jar sky-server/target/sky-server-1.0-SNAPSHOT.jar

   # 或在 IDEA 中运行 com.sky.SkyApplication
   ```

5. 应用未配置端口时使用 Spring Boot 默认的 `8080`；Knife4j 接口文档地址为 `http://localhost:8080/doc.html`。

登录示例（用户名和密码需先在 `employee` 表中存在）：

```http
POST /admin/employee/login
Content-Type: application/json

{
  "username": "<用户名>",
  "password": "<密码>"
}
```

登录成功后返回的 `data.token` 值需放入后续管理端请求的 `token` 请求头。

> 以上启动、登录和 `/doc.html` 访问步骤均**未实际验证**，仓库也缺少运行配置、数据库脚本和初始账号数据。

## 数据库与缺失材料

### 数据库

当前代码直接引用的数据表（依据 Mapper 整理）：

- `employee`
- `category`
- `dish`
- `dish_flavor`
- `setmeal`

其中 `setmeal` 表虽然没有套餐业务接口，但删除分类时会通过 `SetmealMapper.countByCategoryId` 查询该表，因此建表时也必须存在。

`sky-pojo` 的实体类中还有 `orders`、`order_detail`、`shopping_cart`、`address_book`、`user`、`setmeal_dish` 等模型，但当前没有 Mapper 使用它们，属于后续功能的预留。

仓库**未提供**建库语句、建表 SQL 或初始化数据，也**没有**默认管理员账号。需要自行建表；员工密码以 MD5 形式存储，新增员工的默认密码为 `123456`，对应 MD5 值请自行计算后写入。

### 缺失材料

| 材料 | 状态 |
| --- | --- |
| 运行配置 `application*.yml` | 缺失，且被 `.gitignore` 忽略，需本地创建 |
| 数据库 SQL、初始化数据、默认账号 | 缺失 |
| 管理端 Web 前端 | 缺失 |
| 用户端前端 | 缺失 |
| 单元测试 / 集成测试 | 缺失，仓库没有 `src/test`，且 `.gitignore` 忽略 `**/test/` |
| Maven Wrapper | 缺失，需要本机安装 Maven |
| CI 配置、Dockerfile、部署脚本 | 缺失 |
| LICENSE | 缺失 |

## 验证状态与限制

- 本 README 依据当前源码整理；本次提交只新增 README.md，不修改业务代码或依赖。
- 构建、启动、数据库连接、Redis 和 OSS 上传均未在真实环境中执行验证，相关命令和配置示例仅供参考。
- 由于仓库没有前端和 SQL，无法直接从零复现完整的用户操作流程。
- 当前只有管理端接口做了 JWT 鉴权，用户端接口没有鉴权；`logout` 接口仅返回成功，不会让已签发的 JWT 失效。
- 密码使用无盐 MD5 存储，仅适合学习或演示；生产环境应改用带盐的强哈希算法。
