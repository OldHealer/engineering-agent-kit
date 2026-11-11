# Минимальные права внешних сервисов

Это перечень для ручной настройки прав. Kit не читает токены, не выясняет их состав по файлу и не перевыпускает их. Успешный запрос чтения не подтверждает право на публикацию. Учётная запись, ограничения токена и защита веток действуют совместно.

| Сервис | Чтение для ревью | Комментарии | Изменение кода / других данных |
| --- | --- | --- | --- |
| Bitbucket Server / Data Center | Доступ `REPO_READ` к нужным репозиториям, соответствующий доступ PAT | Для REST-комментариев PR API документирует `REPO_READ`; отдельное право push для inline-комментария не требуется. Проверьте ограничения конкретной версии сервера и типа токена | Для push нужен отдельный доступ записи репозитория; для merge действуют права и branch permissions. Не выдавать их только ради комментариев |
| GitHub | Fine-grained PAT, выбранные репозитории, `Contents: read` для кода/Git и `Pull requests: read` для PR | `Pull requests: write` в выбранных репозиториях | Push/API-запись файлов требует `Contents: write`, а некоторые действия — дополнительных прав. Это не требуется для inline-комментариев; branch protection продолжает действовать |
| Jira Server / Data Center | `Browse Projects` только нужных проектов и соответствующий доступ к задачам с issue security | Дополнительно `Add Comments`; права редактирования/удаления комментариев не нужны | Создание, редактирование задач и переходы статусов — отдельные разрешения; текущие MCP kit их не предоставляют |
| Confluence Server / Data Center | Право просмотра нужных spaces и страниц с учётом page restrictions | Для текущего kit не требуется: инструментов записи страниц/комментариев Confluence нет | Не выдавать Add/Edit/Delete/Admin для чтения материалов |

Bitbucket `REPO_READ` не означает «никаких изменений в сервисе»: это право может разрешать комментарии. GitHub `Pull requests: write` также шире, чем одна операция комментария. Список инструментов MCP и подтверждения Codex дополнительно ограничивают доступные действия, но не сужают сам токен на стороне сервиса.

В Jira/Confluence PAT может наследовать права пользователя без отдельной изоляции по проектам. В таком случае ограничение достигается правами отдельной учётной записи/группы на сервисе, а не полем project key в kit. Project key в аргументе MCP — выбор ресурса, а не граница безопасности. Не предполагайте, что личный PAT изолирован одним репозиторием, если соответствующий тип токена этого не поддерживает.

Для получения коммитов предпочтительны отдельные права чтения. Не передавайте Git токен с правом push только потому, что он уже используется другим инструментом. Административные права, управление пользователями, вебхуками и инфраструктурой для этих MCP не нужны.

Официальные источники:

- [Bitbucket REST: комментарии PR](https://developer.atlassian.com/server/bitbucket/rest/v803/api-group-api/): права endpoint зависят от используемой версии API/сервера.
- [GitHub: inline review comments и fine-grained permissions](https://docs.github.com/en/rest/pulls/comments#create-a-review-comment-for-a-pull-request).
- [GitHub: разрешения fine-grained PAT](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens).
- [Jira: project permissions](https://confluence.atlassian.com/adminjiraserver/managing-project-permissions-938847145.html).
- [Confluence: space permissions](https://confluence.atlassian.com/doc/space-permissions-overview-139521.html).
