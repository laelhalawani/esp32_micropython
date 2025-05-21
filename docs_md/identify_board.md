# Guide: How to Identify and Connect Your ESP32-C3 SuperMini Board 

How to identify you have a compatible board (or to choose a different firmware)

## 1. Board Identification by Chip Marking

### 1.1 Rule of Thumb for Silk-Screen Markings

Look for a top-silkscreen string that follows this pattern:

```
ESP32-C3  ?? ?    ??????    ?   ???????    ???????????
```

where each `?` is a placeholder for one or more characters. Concretely, you’ll see something like:

```
ESP32-C3  FH ?   492024    F   H4   P469720   AE02MBL021
```

But the general template is:

```
ESP32-C3   XX Y    ZZZZZZ    T    U?    VVVVVVV    WWWWWWWWWW
```

* **ESP32-C3** – fixed product family
* **XX**    – flash & temperature code (e.g. FH, FN)
* **Y**    – flash size digit (2 / 4 / 8)
* **ZZZZZZ** – six-digit date/lot code
* **T**    – temperature grade (F = –40 °C to +105 °C, N = –40 °C to +85 °C)
* **U?**    – variant or revision suffix (optional)
* **VVVVVVV** – 7-character Espressif trace code
* **WWWWWWWWWW** – vendor-specific module or PCB ID

### 1.2 Breakdown of Fields

| Field                    | Example    | Meaning                                                       |
| ------------------------ | ---------- | ------------------------------------------------------------- |
| **Series**               | ESP32-C3   | SoC family (always “ESP32-C3”)                                |
| **Flash & Temp Code**    | FH         | F = high-temp, H = HBM, N = normal-temp; second letter varies |
| **Flash Size**           | 4          | MB of embedded flash (2, 4, or 8)                             |
| **Date/Lot Code**        | 492024     | Manufacturing date and lot identifier                         |
| **Temp Grade**           | F          | F = –40 °C to +105 °C, N = –40 °C to +85 °C                   |
| **Variant Suffix**       | AZ         | Optional revision or series indicator                         |
| **Espressif Trace Code** | P469720    | Internal QC/tracking code                                     |
| **Vendor Module ID**     | AE02MBL021 | Board-maker’s module or PCB batch code                        |

---

## 2. Board-Level Visual Identification

1. **USB-C Connector**

   * A USB-C port soldered at the PCB edge—no separate USB-UART chip required.
2. **Two Push-Buttons**

   * **BOOT** (also labeled IO0): used during power-up to enter download mode.
   * **RST**: resets the SoC.
3. **Pin Labels (top view)**

   * **Right side:** 5 V | GND | 3V3 | GPIO4 | GPIO3 | GPIO2 | GPIO1 | GPIO0
   * **Left side:** GPIO5 | GPIO6 | GPIO7 | GPIO8 | GPIO9 | GPIO10 | GPIO20 | GPIO21
4. **Power LED**

   * A red LED tied to VBUS that lights whenever the board is powered.

---

## 3. Stable USB Connection Procedure

> **Key:** Hold **BOOT** while plugging in to stop rapid disconnect/reconnect loops.

1. **Unplug** the board from your PC.
2. **Press and hold** the **BOOT** button.
3. **Plug in** the USB-C cable **while still holding** **BOOT**.
4. **Keep holding** for 3–5 seconds after connection.
5. **Release** the **BOOT** button.

After this, open **Device Manager → Ports (COM & LPT)** and confirm you see a steady entry such as **USB Serial Device (COM X)** without flickering or beeping.

---

With these visual checks, marking rules, and connection steps, you can confidently identify any ESP32-C3 SuperMini (or clone) and get it recognized stably on Windows—ready for flashing or further setup.
