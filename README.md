# Target Cam

Zdjęcia tarczy na żądanie z Raspberry Pi 3A+ (kamera IMX708, modem LTE HiLink, dostęp przez Tailscale).
System działa z read-only rootem, więc wypięcie power banku nie psuje karty SD.

## Instalacja (nowe Pi)

```bash
bash install.sh                 # jako zwykły użytkownik
sudo bash ~/harden.sh prepare   # partycja "data" -> /data, zdjęcia i /data/app na /data
sudo bash ~/harden.sh enable    # read-only root, restart
```

## Aktualizacje

```
/  (read-only)   ~/camera/            wersja wbudowana ("legacy"), launcher.py
/data/app/       releases/<wersja>/   pobrane wydania
                 current, previous    symlinki, przełączane atomowo
                 state/               licznik startów, wynik sprawdzenia
```

- `target.service` uruchamia `launcher.py`, który startuje `current`. Jeśli wersja nie wystartuje
  3 razy z rzędu, launcher wraca do `previous`, a gdy i ta nie działa, do wersji wbudowanej.
  Aplikacja zeruje licznik, gdy tylko zacznie odpowiadać.
- Minutę po starcie aplikacja sprawdza (przez LTE) najnowsze wydanie na GitHubie. Nowa wersja
  pojawia się w szczegółach urządzenia (ikona zasięgu) z przyciskiem **Install**. Tam też są
  **Check for update** i **Back to …** (powrót do poprzedniej lub wbudowanej wersji).
- Instalacja: pobranie archiwum, kontrola sha256, rozpakowanie obok, kontrola składni, przełączenie
  symlinka i restart aplikacji (~20 s). Utrata zasilania w trakcie zostawia starą albo nową wersję.
  Root zostaje read-only, nie trzeba trybu `maintenance`.
- Wersja wbudowana zmienia się tylko przez `harden.sh maintenance` + `install.sh` + `harden.sh enable`.

Z konsoli Pi: `python3 ~/camera/updater.py status|check|install [plik.tar.gz]|rollback|legacy`,
potem `sudo systemctl restart target`. `install plik.tar.gz` działa bez internetu.

Aktualizacja może zmieniać tylko pliki aplikacji (`app.py`, `updater.py`, `static/`, `cleanup.sh`).
Nowe pakiety apt, zmiany w unitach systemd lub w `launcher.py` wymagają instalacji przez `install.sh`.

### Prywatne repo

Pi potrzebuje tokenu tylko do odczytu: GitHub → Settings → Developer settings → Fine-grained token,
dostęp tylko do `target-cam`, uprawnienie *Contents: Read-only*. Na Pi:

```bash
echo 'github_pat_...' > /data/app/github_token && chmod 600 /data/app/github_token
```

### Wydanie nowej wersji

1. Zmień `VERSION` (np. `1.1.0`), commit na `main`.
2. `git tag v1.1.0 && git push origin v1.1.0`
3. Workflow *Release* uruchamia testy, buduje `target-cam-1.1.0.tar.gz` + `.sha256` i publikuje wydanie.

## Przejście z dotychczasowej instalacji

```bash
sudo bash ~/harden.sh maintenance   # zapisywalny root, restart
git clone https://github.com/krzysztofgw-pl/target-cam.git && cd target-cam   # lub rozpakuj ZIP z GitHuba
bash install.sh                      # nowa wersja wbudowana + launcher + /data/app
sudo bash ~/harden.sh enable
```
