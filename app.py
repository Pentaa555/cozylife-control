#!/usr/bin/env python3
"""Interfaz local para switches CozyLife."""

from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

try:
    os.environ.setdefault("PYSTRAY_BACKEND", "gtk")
    import pystray
    from PIL import Image, ImageDraw
except (ImportError, ValueError):
    pystray = None
    Image = None
    ImageDraw = None

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "devices.json"
SETTINGS_FILE = BASE_DIR / "settings.json"
AUTOSTART_FILE = Path.home() / ".config" / "autostart" / "cozylife-control.desktop"

DEFAULT_SETTINGS = {
    "start_with_system": False,
    "minimize_on_close": False,
}


def load_settings() -> dict[str, bool]:
    settings = dict(DEFAULT_SETTINGS)
    try:
        with SETTINGS_FILE.open("r", encoding="utf-8") as file:
            stored = json.load(file)
        if isinstance(stored, dict):
            for key in DEFAULT_SETTINGS:
                if key in stored:
                    settings[key] = bool(stored[key])
    except (OSError, json.JSONDecodeError):
        pass
    return settings


def save_settings(settings: dict[str, bool]) -> None:
    temporary = SETTINGS_FILE.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(settings, file, indent=2, ensure_ascii=False)
        file.write("\n")
    temporary.replace(SETTINGS_FILE)


def set_autostart(enabled: bool) -> None:
    if not enabled:
        AUTOSTART_FILE.unlink(missing_ok=True)
        return

    AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
    interpreter = BASE_DIR / ".venv" / "bin" / "python"
    if not interpreter.exists():
        interpreter = Path(sys.executable)
    content = f"""[Desktop Entry]
Type=Application
Name=CozyLife Control
Comment=Control local de switches CozyLife
Exec=\"{interpreter}\" \"{BASE_DIR / 'app.py'}\"
Path=\"{BASE_DIR}\"
Icon=\"{BASE_DIR / 'logo.png'}\"
StartupWMClass=CozyLifeControl
Terminal=false
X-GNOME-Autostart-enabled=true
"""
    AUTOSTART_FILE.write_text(content, encoding="utf-8")


def normalize_mac(value: Any) -> str | None:
    """Devuelve una MAC en formato XX:XX:XX:XX:XX:XX."""
    compact = "".join(char for char in str(value or "") if char.lower() in "0123456789abcdef")
    if len(compact) != 12:
        return None
    compact = compact.lower()
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2))


class CozyLifeError(RuntimeError):
    """Error de comunicación con un dispositivo CozyLife."""


class CozyLifeClient:
    def __init__(self, ip: str, port: int = 5555, timeout: float = 4.0):
        self.ip = ip
        self.port = port
        self.timeout = timeout

    def _exchange(self, command: int, message: dict[str, Any]) -> dict[str, Any]:
        sequence = str(int(time.time() * 1000))
        packet = {
            "pv": 0,
            "cmd": command,
            "sn": sequence,
            "msg": message,
        }
        raw = json.dumps(packet, separators=(",", ":")).encode("utf-8") + b"\r\n"

        try:
            with socket.create_connection((self.ip, self.port), timeout=self.timeout) as sock:
                sock.sendall(raw)
                response = b""
                while b"\r\n" not in response:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
        except OSError as exc:
            raise CozyLifeError(f"No se pudo conectar: {exc}") from exc

        if not response:
            raise CozyLifeError("El switch no devolvió respuesta")

        line = response.split(b"\r\n", 1)[0]
        try:
            payload = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CozyLifeError("Respuesta inválida del switch") from exc

        if payload.get("sn") != sequence:
            raise CozyLifeError("Respuesta no correspondiente al comando")
        return payload

    def get_info(self) -> dict[str, Any]:
        """Consulta identidad, MAC e IP reportadas por el switch."""
        response = self._exchange(0, {})
        info = response.get("msg")
        if not isinstance(info, dict):
            raise CozyLifeError("El switch no devolvió información de identidad")
        return info

    def get_state(self, dpid: int = 1) -> bool:
        response = self._exchange(2, {"attr": [0]})
        data = response.get("msg", {}).get("data", {})
        value = data.get(str(dpid), data.get(dpid, 0))
        return bool(int(value))

    def set_state(self, enabled: bool, dpid: int = 1) -> None:
        response = self._exchange(
            3,
            {
                "attr": [dpid],
                "data": {str(dpid): 1 if enabled else 0},
            },
        )
        if response.get("res") not in (None, 0):
            raise CozyLifeError(f"El switch rechazó el comando: {response}")


def load_devices() -> list[dict[str, Any]]:
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as file:
            devices = json.load(file)
        if not isinstance(devices, list):
            raise ValueError("La configuración debe ser una lista")
        return devices
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        messagebox.showerror("Configuración", f"No se pudo leer devices.json:\n{exc}")
        return []


def save_devices(devices: list[dict[str, Any]]) -> None:
    temporary = CONFIG_FILE.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(devices, file, indent=2, ensure_ascii=False)
        file.write("\n")
    temporary.replace(CONFIG_FILE)


class CozyLifeApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("CozyLife Control")
        self.root.tk.call("tk", "appname", "CozyLifeControl")
        self.window_icon = None
        try:
            self.window_icon = tk.PhotoImage(file=str(BASE_DIR / "logo.png"))
            self.root.iconphoto(True, self.window_icon)
        except tk.TclError:
            self.window_icon = None
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(900, max(520, screen_width - 80))
        height = min(700, max(420, screen_height - 100))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(420, 320)
        self.root.protocol("WM_DELETE_WINDOW", self.handle_close)
        self.root.bind("<Control-q>", lambda _event: self.quit_app())
        self.devices = load_devices()
        self.settings = load_settings()
        self.tray_icon = None
        self.cards: dict[str, dict[str, Any]] = {}
        self.results: queue.Queue[tuple[str, str, bool, Any]] = queue.Queue()

        self._configure_style()
        self._build_ui()
        self.render_devices()
        self.refresh_all()
        self.root.after(100, self.process_results)

    def _configure_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Sans", 18, "bold"))
        style.configure("CardTitle.TLabel", font=("Sans", 13, "bold"))
        style.configure("On.TLabel", foreground="#16803c", font=("Sans", 11, "bold"))
        style.configure("Off.TLabel", foreground="#555555", font=("Sans", 11, "bold"))
        style.configure("Error.TLabel", foreground="#b3261e", font=("Sans", 10))

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, padding=(16, 14, 16, 8))
        header.pack(fill="x")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="CozyLife Control", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        actions = ttk.Frame(header)
        actions.grid(row=0, column=1, sticky="e")
        ttk.Button(actions, text="Actualizar", command=self.refresh_all).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Configuración", command=self.settings_dialog).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Añadir switch", command=self.add_device_dialog).pack(side="left")

        self.status_text = tk.StringVar(value="Listo")
        ttk.Label(self.root, textvariable=self.status_text, padding=(16, 0, 16, 8)).pack(fill="x")

        devices_container = ttk.Frame(self.root, padding=(10, 0, 10, 0))
        devices_container.pack(fill="both", expand=True)
        devices_container.rowconfigure(0, weight=1)
        devices_container.columnconfigure(0, weight=1)

        self.devices_canvas = tk.Canvas(devices_container, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(
            devices_container,
            orient="vertical",
            command=self.devices_canvas.yview,
        )
        self.devices_canvas.configure(yscrollcommand=scrollbar.set)
        self.devices_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.devices_frame = ttk.Frame(self.devices_canvas, padding=6)
        self.devices_window = self.devices_canvas.create_window(
            (0, 0),
            window=self.devices_frame,
            anchor="nw",
        )
        self.devices_frame.bind(
            "<Configure>",
            lambda _event: self.devices_canvas.configure(
                scrollregion=self.devices_canvas.bbox("all")
            ),
        )
        self.devices_canvas.bind(
            "<Configure>",
            lambda event: self.devices_canvas.itemconfigure(
                self.devices_window,
                width=event.width,
            ),
        )
        self.devices_canvas.bind("<Enter>", self._enable_mousewheel)
        self.devices_canvas.bind("<Leave>", self._disable_mousewheel)

        footer = ttk.Frame(self.root, padding=(16, 4, 16, 10))
        footer.pack(fill="x")
        ttk.Label(
            footer,
            text="Doble clic o Editar para modificar un switch",
        ).pack(side="left")

    def _enable_mousewheel(self, _event: tk.Event) -> None:
        self.devices_canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.devices_canvas.bind_all("<Button-4>", self._on_mousewheel)
        self.devices_canvas.bind_all("<Button-5>", self._on_mousewheel)

    def _disable_mousewheel(self, _event: tk.Event) -> None:
        self.devices_canvas.unbind_all("<MouseWheel>")
        self.devices_canvas.unbind_all("<Button-4>")
        self.devices_canvas.unbind_all("<Button-5>")

    def _on_mousewheel(self, event: tk.Event) -> None:
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            delta = -1 * int(event.delta / 120)
        self.devices_canvas.yview_scroll(delta, "units")

    def render_devices(self) -> None:
        for child in self.devices_frame.winfo_children():
            child.destroy()
        self.cards.clear()

        if not self.devices:
            ttk.Label(
                self.devices_frame,
                text="No hay switches configurados. Pulsa «Añadir switch».",
            ).pack(pady=40)
            return

        # Dos columnas fijas; el contenedor tiene scroll vertical para las filas adicionales.
        columns = 2
        self.devices_frame.columnconfigure(0, weight=1, uniform="device-card")
        self.devices_frame.columnconfigure(1, weight=1, uniform="device-card")
        for index, device in enumerate(self.devices):
            device_id = device["id"]
            row, column = divmod(index, columns)
            card = ttk.LabelFrame(self.devices_frame, text=device.get("name", device_id), padding=12)
            card.grid(row=row, column=column, sticky="nsew", padx=6, pady=6)
            self.devices_frame.rowconfigure(row, weight=0)

            title = ttk.Label(card, text=device.get("name", device_id), style="CardTitle.TLabel")
            title.grid(row=0, column=0, columnspan=2, sticky="w")
            ip_label = ttk.Label(card, text=f"IP: {device['ip']}:{device.get('port', 5555)}")
            ip_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))
            mac = normalize_mac(device.get("mac")) or "no disponible"
            mac_label = ttk.Label(card, text=f"MAC: {mac}")
            mac_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 8))

            state = ttk.Label(card, text="Consultando…")
            state.grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 10))
            error = ttk.Label(card, text="", style="Error.TLabel", wraplength=260)
            error.grid(row=4, column=0, columnspan=2, sticky="w")

            for widget in (card, title, ip_label, mac_label, state, error):
                widget.bind(
                    "<Double-Button-1>",
                    lambda _event, d=device: self.edit_device_dialog(d),
                )

            on_button = ttk.Button(card, text="Encender", command=lambda d=device: self.change_state(d, True))
            off_button = ttk.Button(card, text="Apagar", command=lambda d=device: self.change_state(d, False))
            on_button.grid(row=5, column=0, sticky="ew", padx=(0, 4), pady=(8, 0))
            off_button.grid(row=5, column=1, sticky="ew", padx=(4, 0), pady=(8, 0))
            card.columnconfigure(0, weight=1)
            card.columnconfigure(1, weight=1)

            ttk.Button(
                card,
                text="Editar",
                command=lambda d=device: self.edit_device_dialog(d),
            ).grid(row=6, column=0, sticky="w", pady=(8, 0))
            ttk.Button(
                card,
                text="Eliminar",
                command=lambda d=device: self.remove_device(d),
            ).grid(row=6, column=1, sticky="e", pady=(8, 0))
            self.cards[device_id] = {
                "state": state,
                "identity": ip_label,
                "mac": mac_label,
                "error": error,
                "buttons": (on_button, off_button),
            }

    def device_client(self, device: dict[str, Any], *, ip: str | None = None, timeout: float = 4.0) -> CozyLifeClient:
        return CozyLifeClient(
            ip or device["ip"],
            int(device.get("port", 5555)),
            timeout=timeout,
        )

    def local_network_for(self, ip: str) -> ipaddress.IPv4Network:
        """Obtiene la subred local que contiene una IP configurada."""
        target = ipaddress.ip_address(ip)
        try:
            output = subprocess.run(
                ["ip", "-4", "route", "show", "scope", "link"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            for line in output.splitlines():
                first = line.split()[0] if line.split() else ""
                if "/" not in first:
                    continue
                network = ipaddress.ip_network(first, strict=False)
                if target in network:
                    return network
        except (OSError, ValueError):
            pass
        return ipaddress.ip_network(f"{ip}/24", strict=False)

    def scan_for_mac(
        self,
        device: dict[str, Any],
        target_mac: str,
    ) -> tuple[str, str, dict[str, Any]] | None:
        """Busca un switch por MAC dentro de la subred local."""
        network = self.local_network_for(device["ip"])
        addresses = [str(host) for host in network.hosts()]
        port = int(device.get("port", 5555))
        wanted = normalize_mac(target_mac)
        if not wanted:
            return None

        def probe(candidate: str) -> tuple[str, str, dict[str, Any]] | None:
            try:
                info = CozyLifeClient(candidate, port, timeout=0.35).get_info()
                found_mac = normalize_mac(info.get("mac"))
                if found_mac == wanted:
                    return candidate, found_mac, info
            except Exception:
                return None
            return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
            futures = [executor.submit(probe, candidate) for candidate in addresses]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result:
                    for pending in futures:
                        pending.cancel()
                    return result
        return None

    def resolve_device(self, device: dict[str, Any]) -> tuple[str, str | None, dict[str, Any]]:
        """Resuelve la IP actual; usa la MAC si la IP guardada ya cambió."""
        expected_mac = normalize_mac(device.get("mac"))
        first_error: Exception | None = None

        try:
            info = self.device_client(device).get_info()
            current_mac = normalize_mac(info.get("mac"))
            if not expected_mac or current_mac == expected_mac:
                return device["ip"], current_mac, info
            first_error = CozyLifeError("La IP guardada corresponde a otra MAC")
        except Exception as exc:
            first_error = exc

        if expected_mac:
            found = self.scan_for_mac(device, expected_mac)
            if found:
                return found

        raise CozyLifeError(f"No se encontró el switch por su MAC: {first_error}") from first_error

    def refresh_all(self) -> None:
        self.status_text.set("Buscando switches…")
        for device in self.devices:
            self.set_buttons(device["id"], False)
            threading.Thread(target=self.refresh_one, args=(device,), daemon=True).start()

    def refresh_one(self, device: dict[str, Any]) -> None:
        try:
            ip, mac, _info = self.resolve_device(device)
            state = self.device_client(device, ip=ip).get_state(int(device.get("dpid", 1)))
            self.results.put(
                (
                    device["id"],
                    "state",
                    True,
                    {"state": state, "ip": ip, "mac": mac},
                )
            )
        except Exception as exc:  # La interfaz muestra el error al usuario.
            self.results.put((device["id"], "error", False, str(exc)))

    def change_state(self, device: dict[str, Any], enabled: bool) -> None:
        self.set_buttons(device["id"], False)
        self.status_text.set(f"Enviando comando a {device.get('name', device['id'])}…")
        threading.Thread(target=self.set_one, args=(device, enabled), daemon=True).start()

    def set_one(self, device: dict[str, Any], enabled: bool) -> None:
        try:
            ip, mac, _info = self.resolve_device(device)
            self.device_client(device, ip=ip).set_state(enabled, int(device.get("dpid", 1)))
            self.results.put(
                (
                    device["id"],
                    "state",
                    True,
                    {"state": enabled, "ip": ip, "mac": mac},
                )
            )
        except Exception as exc:  # La interfaz muestra el error al usuario.
            self.results.put((device["id"], "error", False, str(exc)))

    def process_results(self) -> None:
        try:
            while True:
                device_id, result_type, success, value = self.results.get_nowait()
                card = self.cards.get(device_id)
                if not card:
                    continue
                self.set_buttons(device_id, True)
                if result_type == "state" and success:
                    details = value if isinstance(value, dict) else {"state": value}
                    state_value = bool(details.get("state"))
                    device = next((item for item in self.devices if item["id"] == device_id), None)
                    identity_changed = False
                    if device:
                        new_ip = details.get("ip")
                        new_mac = normalize_mac(details.get("mac"))
                        if new_ip and device.get("ip") != new_ip:
                            device["ip"] = new_ip
                            identity_changed = True
                        if new_mac and normalize_mac(device.get("mac")) != new_mac:
                            device["mac"] = new_mac
                            identity_changed = True
                        if new_mac:
                            card["mac"].configure(text=f"MAC: {new_mac}")
                        if device and new_ip:
                            card["identity"].configure(
                                text=f"{new_ip}:{device.get('port', 5555)}"
                            )
                        if identity_changed:
                            save_devices(self.devices)
                    card["state"].configure(
                        text="● Encendido" if state_value else "● Apagado",
                        style="On.TLabel" if state_value else "Off.TLabel",
                    )
                    self.set_state_buttons(device_id, state_value)
                    card["error"].configure(text="")
                else:
                    card["state"].configure(text="● Desconectado", style="Error.TLabel")
                    card["error"].configure(text=str(value))
                self.status_text.set("Listo")
        except queue.Empty:
            pass
        self.root.after(100, self.process_results)

    def set_buttons(self, device_id: str, enabled: bool) -> None:
        card = self.cards.get(device_id)
        if not card:
            return
        state = "normal" if enabled else "disabled"
        for button in card["buttons"]:
            button.configure(state=state)

    def set_state_buttons(self, device_id: str, is_on: bool) -> None:
        """Bloquea el comando que ya coincide con el estado actual."""
        card = self.cards.get(device_id)
        if not card:
            return
        on_button, off_button = card["buttons"]
        on_button.configure(state="disabled" if is_on else "normal")
        off_button.configure(state="normal" if is_on else "disabled")

    def create_tray_image(self):
        if Image is None or ImageDraw is None:
            return None
        logo_file = BASE_DIR / "logo.png"
        try:
            logo = Image.open(logo_file).convert("RGBA")
            logo.thumbnail((48, 48), Image.Resampling.LANCZOS)
            image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle(
                (2, 2, 61, 61),
                radius=12,
                fill=(255, 255, 255, 255),
            )
            image.alpha_composite(
                logo,
                ((64 - logo.width) // 2, (64 - logo.height) // 2),
            )
            return image
        except (OSError, ValueError):
            image = Image.new("RGBA", (64, 64), (24, 128, 72, 255))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle(
                (8, 8, 56, 56),
                radius=10,
                fill=(24, 128, 72, 255),
                outline="white",
                width=3,
            )
            draw.rectangle((29, 16, 35, 39), fill="white")
            draw.ellipse((25, 37, 39, 51), fill="white")
            return image

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def tray_action(self, callback, *args):
        """Adapta una acción de Tkinter al callback de pystray."""
        def action(_icon, _item):
            self.root.after(0, callback, *args)
        return action

    def build_tray_menu(self):
        items = [
            pystray.MenuItem(
                "Abrir CozyLife Control",
                self.tray_action(self.show_window),
            ),
        ]

        for device in self.devices:
            device_name = device.get("name", device["id"])
            device_ip = device.get("ip", "sin IP")
            device_id = device["id"]
            items.append(
                pystray.MenuItem(
                    f"{device_name} ({device_ip})",
                    pystray.Menu(
                        pystray.MenuItem(
                            "Encender",
                            self.tray_action(self.tray_change_state, device, True),
                        ),
                        pystray.MenuItem(
                            "Apagar",
                            self.tray_action(self.tray_change_state, device, False),
                        ),
                        pystray.MenuItem(
                            "Abrir tarjeta",
                            self.tray_action(self.show_device_window, device),
                        ),
                    ),
                )
            )

        items.extend(
            [
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "Actualizar todos",
                    self.tray_action(self.refresh_all),
                ),
                pystray.MenuItem(
                    "Salir",
                    self.tray_action(self.quit_app),
                ),
            ]
        )
        return pystray.Menu(*items)

    def update_tray_menu(self) -> None:
        if self.tray_icon is not None:
            self.tray_icon.menu = self.build_tray_menu()

    def tray_change_state(self, device: dict[str, Any], enabled: bool) -> None:
        self.status_text.set(
            f"Enviando comando a {device.get('name', device['id'])}…"
        )
        threading.Thread(
            target=self.set_one,
            args=(device, enabled),
            daemon=True,
        ).start()

    def show_device_window(self, device: dict[str, Any]) -> None:
        self.show_window()
        self.status_text.set(f"Switch seleccionado: {device.get('name', device['id'])}")

    def hide_to_tray(self) -> None:
        if pystray is None:
            messagebox.showwarning(
                "Bandeja no disponible",
                "Falta pystray. Ejecuta la aplicación usando el lanzador del menú.",
                parent=self.root,
            )
            self.root.iconify()
            return

        if self.tray_icon is None:
            self.tray_icon = pystray.Icon(
                "cozylife-control",
                self.create_tray_image(),
                "CozyLife Control",
                self.build_tray_menu(),
            )
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        else:
            self.update_tray_menu()
        self.root.withdraw()

    def handle_close(self) -> None:
        if self.settings.get("minimize_on_close", False):
            self.hide_to_tray()
        else:
            self.quit_app()

    def quit_app(self) -> None:
        if self.tray_icon is not None:
            self.tray_icon.stop()
            self.tray_icon = None
        self.root.destroy()

    def settings_dialog(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Configuración")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

        start_var = tk.BooleanVar(value=self.settings.get("start_with_system", False))
        minimize_var = tk.BooleanVar(value=self.settings.get("minimize_on_close", False))

        content = ttk.Frame(dialog, padding=16)
        content.pack(fill="both", expand=True)
        ttk.Label(content, text="Preferencias de CozyLife Control", style="CardTitle.TLabel").pack(
            anchor="w", pady=(0, 12)
        )
        ttk.Checkbutton(
            content,
            text="Iniciar la aplicación al prender el equipo",
            variable=start_var,
        ).pack(anchor="w", pady=4)
        ttk.Checkbutton(
            content,
            text="Al cerrar, ocultar en la bandeja del sistema (junto al reloj)",
            variable=minimize_var,
        ).pack(anchor="w", pady=4)
        ttk.Label(
            content,
            text="También puedes salir completamente con Ctrl+Q.",
        ).pack(anchor="w", pady=(10, 4))

        def save() -> None:
            new_settings = {
                "start_with_system": bool(start_var.get()),
                "minimize_on_close": bool(minimize_var.get()),
            }
            try:
                set_autostart(new_settings["start_with_system"])
                save_settings(new_settings)
            except OSError as exc:
                messagebox.showerror("No se pudo guardar", str(exc), parent=dialog)
                return
            self.settings = new_settings
            dialog.destroy()
            self.status_text.set("Configuración guardada")

        buttons = ttk.Frame(content)
        buttons.pack(anchor="e", pady=(14, 0))
        ttk.Button(buttons, text="Cancelar", command=dialog.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Guardar", command=save).pack(side="left")

        dialog.update_idletasks()
        width = dialog.winfo_width()
        height = dialog.winfo_height()
        x = self.root.winfo_rootx() + max((self.root.winfo_width() - width) // 2, 0)
        y = self.root.winfo_rooty() + max((self.root.winfo_height() - height) // 2, 0)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.deiconify()
        dialog.lift()
        dialog.focus_force()

    def add_device_dialog(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Añadir switch CozyLife")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

        fields = [
            ("Nombre", "Nuevo switch"),
            ("IP", "192.168.0.54"),
            ("Puerto", "5555"),
            ("DPID", "1"),
        ]
        entries: dict[str, ttk.Entry] = {}
        for row, (label, default) in enumerate(fields):
            ttk.Label(dialog, text=label).grid(row=row, column=0, padx=12, pady=6, sticky="w")
            entry = ttk.Entry(dialog, width=28)
            entry.insert(0, default)
            entry.grid(row=row, column=1, padx=12, pady=6)
            entries[label] = entry

        def save() -> None:
            name = entries["Nombre"].get().strip()
            ip = entries["IP"].get().strip()
            if not name or not ip:
                messagebox.showwarning("Datos incompletos", "Indica un nombre y una IP.", parent=dialog)
                return
            try:
                port = int(entries["Puerto"].get())
                dpid = int(entries["DPID"].get())
                if not 1 <= port <= 65535 or dpid < 1:
                    raise ValueError
            except ValueError:
                messagebox.showwarning(
                    "Datos inválidos",
                    "Puerto debe estar entre 1 y 65535; DPID debe ser positivo.",
                    parent=dialog,
                )
                return

            device_id = f"device-{int(time.time() * 1000)}"
            device = {
                "id": device_id,
                "name": name,
                "ip": ip,
                "port": port,
                "dpid": dpid,
            }
            try:
                self.devices.append(device)
                save_devices(self.devices)
            except (OSError, TypeError, ValueError) as exc:
                self.devices.pop()
                messagebox.showerror("No se pudo guardar", str(exc), parent=dialog)
                return

            dialog.destroy()
            self.render_devices()
            self.refresh_all()
            self.update_tray_menu()
            self.status_text.set(f"Switch añadido: {name}")
            messagebox.showinfo(
                "Switch añadido",
                f"{name} fue guardado correctamente.\n\nIP: {ip}\nPuerto: {port}",
                parent=self.root,
            )

        buttons = ttk.Frame(dialog)
        buttons.grid(row=len(fields), column=0, columnspan=2, pady=12)
        ttk.Button(buttons, text="Cancelar", command=dialog.destroy).pack(side="left", padx=5)
        ttk.Button(buttons, text="Guardar switch", command=save).pack(side="left", padx=5)

        dialog.update_idletasks()
        width = dialog.winfo_width()
        height = dialog.winfo_height()
        x = self.root.winfo_rootx() + max((self.root.winfo_width() - width) // 2, 0)
        y = self.root.winfo_rooty() + max((self.root.winfo_height() - height) // 2, 0)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.deiconify()
        dialog.lift()
        dialog.focus_force()
        entries["Nombre"].focus_set()

    def edit_device_dialog(self, device: dict[str, Any]) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Editar switch: {device.get('name', device['id'])}")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

        fields = [
            ("Nombre", str(device.get("name", ""))),
            ("IP", str(device.get("ip", ""))),
            ("Puerto", str(device.get("port", 5555))),
            ("DPID", str(device.get("dpid", 1))),
        ]
        entries: dict[str, ttk.Entry] = {}
        for row, (label, default) in enumerate(fields):
            ttk.Label(dialog, text=label).grid(row=row, column=0, padx=12, pady=6, sticky="w")
            entry = ttk.Entry(dialog, width=28)
            entry.insert(0, default)
            entry.grid(row=row, column=1, padx=12, pady=6)
            entries[label] = entry

        def save() -> None:
            name = entries["Nombre"].get().strip()
            ip = entries["IP"].get().strip()
            if not name or not ip:
                messagebox.showwarning("Datos incompletos", "Indica un nombre y una IP.", parent=dialog)
                return
            try:
                port = int(entries["Puerto"].get())
                dpid = int(entries["DPID"].get())
                if not 1 <= port <= 65535 or dpid < 1:
                    raise ValueError
            except ValueError:
                messagebox.showwarning(
                    "Datos inválidos",
                    "Puerto debe estar entre 1 y 65535; DPID debe ser positivo.",
                    parent=dialog,
                )
                return

            original = dict(device)
            device.update({"name": name, "ip": ip, "port": port, "dpid": dpid})
            try:
                save_devices(self.devices)
            except (OSError, TypeError, ValueError) as exc:
                device.clear()
                device.update(original)
                messagebox.showerror("No se pudo guardar", str(exc), parent=dialog)
                return

            dialog.destroy()
            self.render_devices()
            self.refresh_all()
            self.update_tray_menu()
            self.status_text.set(f"Switch actualizado: {name}")
            messagebox.showinfo(
                "Switch actualizado",
                f"{name} fue actualizado correctamente.",
                parent=self.root,
            )

        buttons = ttk.Frame(dialog)
        buttons.grid(row=len(fields), column=0, columnspan=2, pady=12)
        ttk.Button(buttons, text="Cancelar", command=dialog.destroy).pack(side="left", padx=5)
        ttk.Button(buttons, text="Guardar cambios", command=save).pack(side="left", padx=5)

        dialog.update_idletasks()
        width = dialog.winfo_width()
        height = dialog.winfo_height()
        x = self.root.winfo_rootx() + max((self.root.winfo_width() - width) // 2, 0)
        y = self.root.winfo_rooty() + max((self.root.winfo_height() - height) // 2, 0)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.deiconify()
        dialog.lift()
        dialog.focus_force()
        entries["Nombre"].focus_set()
        entries["Nombre"].selection_range(0, tk.END)

    def remove_device(self, device: dict[str, Any]) -> None:
        if not messagebox.askyesno("Eliminar switch", f"¿Eliminar {device.get('name', device['id'])}?"):
            return
        self.devices = [item for item in self.devices if item["id"] != device["id"]]
        save_devices(self.devices)
        self.render_devices()
        self.update_tray_menu()
        self.status_text.set("Switch eliminado")


def main() -> None:
    root = tk.Tk()
    CozyLifeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
