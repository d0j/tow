# Установка TOW

По шагам — для тех, кто ни разу не ставил программу с GitHub. Выберите свою систему, выполните шаги, и TOW откроется в
браузере. English version: [../install.md](../install.md).

- [Windows](#windows)
- [macOS](#macos)
- [Linux](#linux)
- [Где лежит TOW](#где-лежит-tow)
- [Запуск, остановка, обновление](#запуск-остановка-обновление)
- [Сохраните мастер-ключ](#сохраните-мастер-ключ)
- [Как удалить TOW](#как-удалить-tow)
- [Если что-то пошло не так](#если-что-то-пошло-не-так)
- [Ручная установка через git](#ручная-установка-через-git)

TOW хранит всё в одной папке: саму программу, свой Python, ваши настройки и данные. Он ничего не устанавливает в
Windows, macOS или Linux, и права администратора ему не нужны.

## Windows

Windows 10 или 11, 64-битная, на процессоре x64 (Intel или AMD). Windows на ARM не поддерживается: архив и
установщик сделаны для x64.

### Архив zip (проще всего)

1. Скачайте [TOW-windows-x64.zip](https://github.com/d0j/tow/releases/latest/download/TOW-windows-x64.zip) (около
   50 МБ).
2. По желанию, это уберёт один вопрос позже: правой кнопкой по архиву → **Свойства** → отметьте **Разблокировать** →
   **ОК**.
3. Правой кнопкой по архиву → **Извлечь все…** → выберите, куда распаковать, например `C:\` или папку
   пользователя → **Извлечь**. В архиве уже есть папка `TOW`, поэтому появится `C:\TOW` (или `TOW` в папке
   пользователя), в ней — `Start TOW.cmd`.
4. Дважды щёлкните **Start TOW.cmd**.
   - Windows может написать **«Система Windows защитила ваш компьютер»**. Нажмите **Подробнее**, затем **Выполнить в
     любом случае**. Так Windows говорит о любой программе не из Microsoft Store и без платной подписи.
   - Может появиться и вопрос **«Запустить этот файл?»** — нажмите **Запустить**.
5. Откроется чёрное окно с надписью **«Preparing TOW…»**. Первый запуск занимает минуту-две, интернет не нужен.
6. Браузер откроет **<http://127.0.0.1:8787>**. Это и есть TOW.
7. Чёрное окно покажет, где лежит **мастер-ключ**, и подождёт: сразу скопируйте этот файл (см.
   [Сохраните мастер-ключ](#сохраните-мастер-ключ)), затем нажмите любую клавишу. Окно закроется, TOW продолжит
   работать.

В следующий раз снова дважды щёлкните **Start TOW.cmd**: если TOW уже работает, просто откроется страница.

### Одна строка в PowerShell

То же самое без щелчков в Проводнике. Откройте PowerShell: правой кнопкой по кнопке **Пуск** → **Терминал** (или
**Windows PowerShell**). Вставьте строку и нажмите Enter:

```powershell
irm https://github.com/d0j/tow/releases/latest/download/install.ps1 | iex
```

Она скачает архив, сверит его с файлом контрольных сумм выпуска, распакует в папку `TOW` в папке пользователя и
запустит. С параметрами (другая папка, запуск вместе с Windows, другой порт):

```powershell
& ([scriptblock]::Create((irm https://github.com/d0j/tow/releases/latest/download/install.ps1))) -Dir D:\TOW -Autostart -Port 8788
```

Параметры: `-Dir ПАПКА`, `-Version v1.25.0` (определённый выпуск вместо последнего), `-Port N` и `-Autostart`; для
удаления — `-Uninstall` с `-Yes` (не спрашивать), `-Purge` и `-AdoptData` (см. [Как удалить TOW](#как-удалить-tow)).
`-Dir`, `-Version` и `-Port` можно задать и переменными `TOW_INSTALL_DIR`, `TOW_VERSION` и `TOW_INSTALL_PORT`.

## macOS

Mac с процессором Apple (M1 и новее). На Mac с Intel TOW работает, только если установлены инструменты разработчика
(см. [Если что-то пошло не так](#если-что-то-пошло-не-так)).

1. Откройте **Терминал**: нажмите **Cmd + пробел**, наберите `Терминал` (или `Terminal`), нажмите **Enter**.
2. Вставьте эту строку и нажмите **Enter**:

   ```sh
   curl -LsSf https://github.com/d0j/tow/releases/latest/download/install.sh | sh
   ```

   Это займёт несколько минут: скачаются TOW, его собственный Python и библиотеки — в папку `TOW` в вашей домашней
   папке. В конце будет сказано, где лежит **мастер-ключ**, — скопируйте его (см.
   [Сохраните мастер-ключ](#сохраните-мастер-ключ)).
3. Запустите TOW: в Finder выберите **Переход → Домой**, откройте **TOW**, дважды щёлкните **Start TOW.command**.
   Откроется окно Терминала, TOW запустится, а браузер откроет **<http://127.0.0.1:8787>**. Окно Терминала можно
   закрыть — TOW продолжит работать.

Чтобы TOW запускался при каждом входе в систему, добавьте `-s -- --autostart` в строку из шага 2 (после `sh`) или
позже выполните `~/TOW/app/scripts/tow autostart on`.

Не кладите папку `TOW` и папки, куда скачиваются торренты, в **Документы**, **Рабочий стол** и **Загрузки**: macOS не
даёт фоновым программам их читать.

## Linux

Любой 64-битный дистрибутив (x86-64 или ARM, в том числе Raspberry Pi с 64-битной системой), где есть `curl` (или
`wget`) и `tar`.

1. Откройте терминал: в Ubuntu нажмите **Ctrl + Alt + T**; в других системах найдите **Терминал** в меню приложений.
2. Вставьте эту строку и нажмите **Enter**:

   ```sh
   curl -LsSf https://github.com/d0j/tow/releases/latest/download/install.sh | sh
   ```

   TOW установится в `~/TOW`, и будет сказано, где лежит **мастер-ключ**, — скопируйте его.
3. Запустите TOW: `~/TOW/start-tow`. Браузер откроет **<http://127.0.0.1:8787>** (на сервере без экрана будет
   показан адрес).

Параметры пишутся после `sh -s --`, например:

```sh
curl -LsSf https://github.com/d0j/tow/releases/latest/download/install.sh | sh -s -- --autostart --desktop
```

| Параметр | Что делает |
|---|---|
| `--autostart` | запускать TOW при входе в систему (пользовательская служба systemd) |
| `--desktop` | добавить TOW в меню приложений |
| `--dir ПАПКА` | установить не в `~/TOW`, а в другую папку |
| `--port N` | другой порт вместо 8787 |
| `--version v1.25.0` | определённый выпуск вместо последнего |
| `--uninstall` | удалить TOW (см. [Как удалить TOW](#как-удалить-tow)); с `--yes` — без вопроса, с `--purge` — вместе с данными |

`--dir`, `--version` и `--port` можно задать и переменными `TOW_INSTALL_DIR`, `TOW_VERSION` и `TOW_INSTALL_PORT`
(например, `curl … | TOW_INSTALL_DIR=/srv/tow sh`).

На сервере, чтобы TOW запускался при загрузке, ещё до входа в систему: `~/TOW/app/scripts/tow autostart on
--without-login`, затем `sudo loginctl enable-linger $USER`. Как открыть страницу с другого компьютера — в разделе
[Доступ с других устройств](../../README.ru.md#доступ-с-других-устройств).

## Где лежит TOW

```
TOW/
  Start TOW.cmd / Start TOW.command / start-tow     запустить (и открыть страницу)
  Stop TOW.cmd  / Stop TOW.command  / stop-tow      остановить
  Update TOW.cmd / Update TOW.command / update-tow  обновить до последнего выпуска
  README.txt       (zip для Windows) эти шаги кратко
  config.yaml      настройки
  data/            ваши раздачи, история, журналы
  keys/master.key  мастер-ключ
  backup/          ночные копии, снимки перед обновлениями
  app/             сама программа
  runtime/         её Python и библиотеки
```

Папку можно перенести или скопировать целиком: файл запуска сам подготовит её на новом месте. Если TOW запускается
вместе с компьютером, включите автозапуск заново на новом месте (`app\scripts\tow.cmd autostart on`, в macOS и Linux —
`app/scripts/tow autostart on`): прежний указывает на старую папку.

## Запуск, остановка, обновление

| | Windows | macOS | Linux |
|---|---|---|---|
| Запустить, открыть страницу | `Start TOW.cmd` | `Start TOW.command` | `~/TOW/start-tow` |
| Остановить | `Stop TOW.cmd` | `Stop TOW.command` | `~/TOW/stop-tow` |
| Запускать вместе с компьютером | `app\scripts\tow.cmd autostart on` | `~/TOW/app/scripts/tow autostart on` | так же |
| Обновить до последнего выпуска | `Update TOW.cmd` | `Update TOW.command` | `~/TOW/update-tow` |
| Вернуться к прежнему выпуску | PowerShell в папке TOW: `& ".\Update TOW.cmd" v1.25.0` | `~/TOW/"Update TOW.command" v1.25.0` | `~/TOW/update-tow v1.25.0` |

Обновление останавливает TOW, сохраняет копию данных и настроек в `backup/`, ставит новую версию, запускает её и
проверяет. Если что-то не так, прежняя версия возвращается сама. Для обновления нужен интернет. Если у вас уже
последний выпуск, обновление скажет об этом и ничего не сделает. В Windows оно ещё кладёт в папку TOW
`Start TOW.cmd`, `Stop TOW.cmd` и `Update TOW.cmd` новой версии (правки, сделанные в них вручную, не сохраняются). Вернуться
можно только к версии, которая читает ваши данные: после запуска v1.23 — не дальше v1.23.0 (обновление
скажет об этом и ничего не изменит).

## Сохраните мастер-ключ

При первом запуске появляется `TOW/keys/master.key`. Им зашифрованы все сохранённые пароли и токены. В резервные копии
и файлы TOW (`.towx`) он намеренно не входит. **Скопируйте его на флешку или в менеджер паролей прямо сейчас.** Без него
сохранённые пароли не прочитать — даже из резервной копии.

## Как удалить TOW

| | |
|---|---|
| Windows | `& ([scriptblock]::Create((irm https://github.com/d0j/tow/releases/latest/download/install.ps1))) -Uninstall` |
| macOS, Linux | `curl -LsSf https://github.com/d0j/tow/releases/latest/download/install.sh \| sh -s -- --uninstall` |

Перед удалением будет задан вопрос; автозапуск выключится, TOW остановится. Папки `data`, `keys`, `backup` и файл
`config.yaml` останутся, если вы не ответите иначе (или не добавите `-Purge` / `--purge`). `-Dir` / `--dir` указывает
папку, если она не стандартная (`TOW` в папке пользователя): для архива, распакованного в `C:\TOW`, добавьте
`-Dir C:\TOW`. Можно и просто остановить TOW, выключить автозапуск и удалить папку.

Оставшееся подхватывается снова: если позже установить TOW в ту же папку, данные, ключ и настройки сохранятся.
Чтобы удалить оставшееся, выполните команду удаления ещё раз с `-Purge` / `--purge`.
Установщик помечает папку TOW, чтобы при удалении не принять чужие `data` и `config.yaml` за TOW. Для данных,
оставшихся после старого установщика без такой отметки, используйте `-AdoptData` / `--adopt-data` только убедившись,
что это именно ваша прежняя папка TOW.

## Если что-то пошло не так

| Что вы видите | Что делать |
|---|---|
| «Система Windows защитила ваш компьютер» | **Подробнее** → **Выполнить в любом случае**. Или до распаковки: архив → **Свойства** → **Разблокировать**. |
| «TOW did not start», а в `data/logs/run.log` сказано, что порт 8787 занят | Порт занят другой программой (часто — вторым TOW). Откройте `config.yaml` в папке TOW, замените `port: 8787` на `port: 8788`, запустите снова и откройте <http://127.0.0.1:8788>. |
| Страница не открывается | Подождите минуту и откройте <http://127.0.0.1:8787> вручную. `app\scripts\tow.cmd status` в папке TOW (macOS, Linux: `~/TOW/app/scripts/tow status`) покажет, работает ли TOW; причина неудачного запуска — в `TOW/data/logs/run.log`. |
| Установщик не может ничего скачать (сеть на работе, антивирус) | Ему нужны `github.com` и `raw.githubusercontent.com` (и узлы `*.githubusercontent.com`, с которых GitHub отдаёт файлы), а в macOS и Linux ещё `pypi.org` и `files.pythonhosted.org`. За прокси сначала задайте `HTTPS_PROXY`. Архиву для Windows интернет при первом запуске не нужен. |
| macOS спрашивает, можно ли Терминалу или Python открыть папку | Разрешите или держите TOW и папки загрузок вне Документов, Рабочего стола и Загрузок. |
| На Mac с Intel установка останавливается на «cryptography» | Установите инструменты разработчика (`xcode-select --install`) и Rust (<https://rustup.rs>), затем запустите установщик снова. |
| «TOW is already installed» | Обновите TOW файлом обновления или сначала удалите его. |
| «… is not empty: choose another folder» | В папке есть другие файлы. Установите TOW в новую или пустую папку (`-Dir` / `--dir`). |
| «… has data without a TOW install marker» | В папке данные старого установщика TOW или чужие `data` и `config.yaml`. Если это ваша прежняя папка TOW, запустите установщик снова с `-AdoptData` / `--adopt-data`; иначе выберите другую папку. |
| «… longer than 110 characters and Windows long paths are off» | Путь к папке TOW длиннее 110 символов, а длинные пути в Windows выключены: подготовка TOW там не удастся. Перенесите папку в короткий путь, например `C:\TOW` (установщик: `-Dir C:\TOW`), или включите длинные пути (`LongPathsEnabled`). |
| «TOW was not started: an update was cut off while it replaced the code …» (или «TOW cannot run: …», «TOW не запущен: обновление прервалось…») | Обновление остановилось на полпути: выключился компьютер или программу обновления завершили принудительно. Запустите обновление ещё раз (`Update TOW.cmd`, `Update TOW.command` или `update-tow`): сначала оно вернёт прежнюю версию, затем установит выпуск. |
| «TOW was not started: its code is incomplete, "…\app\scripts\tow-start.cmd" is missing» (или «TOW cannot run: its code is incomplete …») | Части файлов кода TOW нет (антивирус, программа очистки, незаконченное копирование). Откройте терминал в папке TOW и запустите `"Update TOW.cmd"` с вашей версией, например `& ".\Update TOW.cmd" v1.28.1` в PowerShell: выпуск, названный по версии, ставится заново, данные и настройки остаются. |
| Повторный запуск `Update TOW.cmd` после прерванного обновления пишет «can't open file …\app\scripts\update.py» | В папке TOW остался файл обновления из более старого zip (до 1.28.1 включительно обновление его не заменяло). Откройте PowerShell в папке TOW и запустите копию программы обновления, которую оставило прерванное обновление, через Python TOW (папка в `runtime\python`, имя которой начинается с `cpython-3.` и содержит три числа): `& ".\runtime\python\cpython-3.14.8-windows-x86_64-none\python.exe" runtime\update.py --ref latest`. Оно вернёт прежнюю версию, установит выпуск и запишет текущие файлы запуска. |
| «TOW: TOW_ROOT=… is not the folder of this TOW, so it is ignored» | Переменная `TOW_ROOT` осталась после переноса или от другой установки. TOW берёт свою папку; удалите переменную (Windows: **Изменение переменных среды текущего пользователя**; Linux, macOS: профиль оболочки). |
| «TOW's environment still belongs to the folder TOW was moved or copied from» (или «TOW не запущен: его окружение запускает код из …») | Папку перенесли или скопировали. Дважды щёлкните файл запуска — он её подготовит — или выполните `app\scripts\tow.cmd setup` (macOS, Linux: `~/TOW/app/scripts/tow setup`). |
| После переноса или копирования папки TOW не запускается вместе с компьютером | Автозапуск указывает на старую папку. Включите его в новой: `app\scripts\tow.cmd autostart on` (macOS, Linux: `app/scripts/tow autostart on`). После переноса об этом скажут и Диагностика (в конце Настроек), и `tow status`. |
| Страница, на которой только «untrusted host» | TOW не отвечает по этому адресу. На самом компьютере откройте <http://127.0.0.1:8787> или <http://localhost:8787>; с другого устройства — IP-адрес компьютера, его простое имя или `имя.local`, но не `имя.lan` и не имя MagicDNS в Tailscale ([Доступ с других устройств](../../README.ru.md#доступ-с-других-устройств)). |

## Ручная установка через git

Для разработчиков и серверов: клон, который `deploy.ps1` / `tow update` переключают между метками выпусков через
git. Нужны [git](https://git-scm.com/downloads) и [uv](https://docs.astral.sh/uv/) версии 0.12 или новее; `setup` ставит
Python 3.14 и библиотеки внутрь папки TOW.

**Windows** (PowerShell):

```powershell
winget install --id Git.Git -e
winget install --id astral-sh.uv -e
# откройте новое окно терминала, затем:
git clone https://github.com/d0j/tow "$HOME\TOW\app"
cd "$HOME\TOW\app"
Copy-Item config.example.yaml ..\config.yaml
.\scripts\tow.cmd setup
.\scripts\tow.cmd run
```

Запуск вместе с Windows (одна задача, без окна консоли): `.\scripts\tow.cmd autostart on`.

**Linux:**

```sh
sudo apt install git          # или: dnf install git / pacman -S git
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/d0j/tow ~/TOW/app
cd ~/TOW/app
cp config.example.yaml ../config.yaml
./scripts/tow setup
./scripts/tow run
```

Запуск при входе (пользовательский юнит systemd): `./scripts/tow autostart on`. Запуск при загрузке, без входа:
`./scripts/tow autostart on --without-login`, затем `sudo loginctl enable-linger $USER`.

**macOS:**

```sh
brew install git uv           # или: xcode-select --install и установщик uv выше
git clone https://github.com/d0j/tow ~/TOW/app
cd ~/TOW/app
cp config.example.yaml ../config.yaml
./scripts/tow setup
./scripts/tow run
```

Запуск при входе (LaunchAgent): `./scripts/tow autostart on`.

Обновить клон: `.\scripts\deploy.ps1 -Ref v1.25.0` в Windows (Windows PowerShell или PowerShell 7, в папке `app`);
в Linux и macOS `./scripts/tow update --ref v1.25.0` покажет команду. Если PowerShell отвечает, что выполнение
сценариев отключено в этой системе, запустите так (сценарии разрешаются только для этой одной команды):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\deploy.ps1 -Ref v1.25.0
```

Подробности (англ.): [PORTABLE.md](../PORTABLE.md).
