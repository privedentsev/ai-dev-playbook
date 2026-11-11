# PB-061: команды воспроизведения и граница стенда

Среда: macOS (Darwin 27), Python 3.14, `sandbox-exec` доступен.
Отдельная OS identity недоступна — граница прав агента не доказана:
`verifier probe` честно возвращает `capability=local` (AC-03,
защищённая эксплуатация не заявлена).

## Адресные проверки

```sh
python3 -m unittest discover -s tests -p "test_checks_trust.py" -v   # 32 теста
python3 -m unittest discover -s tests -p "test_checks_*.py"          # 243 теста
python3 ai-dev-playbook/scripts/playbook.py index
python3 ai-dev-playbook/scripts/playbook.py check --ready
python3 ai-dev-playbook/scripts/playbook.py adopt . --force --apply   # self-manifest
```

## Ручной прогон протокола (smoke на временном проекте)

```sh
playbook.py --root $PROJ verifier init --home $HOME_DIR --policy $PROJ/checks-policy.json --actor Devin
playbook.py --root $PROJ verifier approve --home $HOME_DIR --actor Devin
playbook.py --root $PROJ verifier run --home $HOME_DIR        # sandbox=да, receipt выдан
playbook.py --root $PROJ verifier verify --home $HOME_DIR --receipt $RC   # accepted
playbook.py --root $PROJ verifier gate --home $HOME_DIR       # покрыто 1/1, capability=local
playbook.py --root $PROJ verifier aggregate --home $HOME_DIR --receipts $RC
```

Факты smoke: подписанная квитанция принята, gate покрыл обязательство,
aggregate подтвердил неизменённые входы; подделанная подпись и чужой
ключ отклонены (`signature: подпись не совпадает (forgery)`).

## Ограничения

- sandbox-exec — реальная граница для подпроцесса на этом стенде
  (deny file-read*/file-write* на дом подтверждена командой и тестом);
  для агента отдельной OS identity нет — `boundary=none`, `local`.
- Потребление gate в lifecycle/release-ready — PB-062; opt-in
  self-project — PB-063. Прежний контракт verify не изменён.

## Исправления AUDIT-02 (EXEC-03)

Замечания: A-01 (self-reported ack-обход), A-05 (probe каталогов → EEXIST).

- A-01: `checks_verifier.run` отклоняет политику с исполняемыми
  `kind=test` проверками до исполнения — события `python-unittest`
  self-reported, доказанное происхождение недостижимо. Квитанции
  выдаются только для наблюдаемых command-результатов; gate остаётся
  непокрытым. Локальный `checks run` — диагностика без изменений.
- A-05: `_writable(directory=True)` создаёт уникальный `.probe-*`
  marker внутри каталога (дом, родительский каталог, keys) и удаляет
  только его; EACCES/EPERM → False, прочие OSError → None.

```sh
python3 -m unittest tests.test_checks_trust -v   # 43 теста (было 39)
python3 -m unittest test_checks_contract test_checks_graph \
    test_checks_inventory test_checks_run test_checks_select \
    test_checks_trust                              # регрессии checks
```
