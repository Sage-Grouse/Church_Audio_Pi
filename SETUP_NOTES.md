# Church Audio Pi — Setup Guide

Step-by-step installation for a Raspberry Pi 5. For an overview of what the project does, see [README.md](README.md). Tested on Debian 12 (bookworm) with Python 3.11.2.

Commands assume the Linux user is `church-pi` (UID 1000). If yours differs, change it in the service file, the sudoers entry and the paths below.

## 0. Secrets

Real passwords go only in `.env` (set to `chmod 600`), never in notebooks, notes or Git. If a password was ever pasted into a shared file, rotate it.

Gmail requires a 16-character App Password, not your normal password. Create one at https://myaccount.google.com/apppasswords (2-Step Verification must be on).

## 1. Base setup (one time)

```bash
sudo apt update
sudo apt install -y ffmpeg python3-tk python3-venv

python3 -m venv ~/church_audio_env
source ~/church_audio_env/bin/activate

pip install -r requirements.txt
```

## 2. Set up the two USB drives

Two drives are plugged in at once (the Pi 5 has four USB ports):

- **Yamaha recording drive.** Whatever FAT32/exFAT drive is used that week. No special label is needed. The udev rule fires for any inserted drive except the output drive, and the script confirms the drive holds a recent WAV file before doing anything.
- **Output/archive drive.** Plugged in permanently and holds the finished MP3s in `Music/`. MP3s are small, so a 32-64 GB drive will last years. It must be labeled `CHURCHMUSIC` and must have a partition (it shows up as `sdY1`, not just `sdY`), because that is how the Pi tells it apart from the recording drive.

Volume labels on FAT are limited to 11 characters, which is why the label is `CHURCHMUSIC`.

**Format and label the output drive.** exFAT is recommended (no 4 GB file limit, readable on Windows and Mac). To format fresh, which erases the drive:

```bash
sudo apt install -y exfatprogs
lsblk -f                  # find the partition, e.g. sdb1
sudo mkfs.exfat -n CHURCHMUSIC /dev/sdY1
```

To only relabel a drive you want to keep:

```bash
sudo umount /dev/sdY1
sudo fatlabel /dev/sdY1 CHURCHMUSIC       # FAT32
sudo exfatlabel /dev/sdY1 CHURCHMUSIC     # exFAT
```

**Mount it permanently.** Find its UUID:

```bash
sudo blkid /dev/sdY1
```

Create the mount points. `/mnt/church_music` is the archive drive, and `/mnt/church_usb` is where the service mounts the recording drive:

```bash
sudo mkdir -p /mnt/church_music /mnt/church_usb
sudo nano /etc/fstab
```

Add this line, using your real UUID (`nofail` lets the Pi boot even if the drive is missing):

```
UUID=XXXX-XXXX  /mnt/church_music  exfat  defaults,nofail,uid=1000,gid=1000  0  0
```

Mount it without rebooting and confirm:

```bash
sudo systemctl daemon-reload
sudo mount -a
lsblk -f        # /mnt/church_music should show CHURCHMUSIC, mounted
```

## 3. Install the project files

| File | Goes to |
|---|---|
| `audio_exporter.py` | `/home/church-pi/Documents/audio_exporter.py` |
| `.env` (copied from `.env.example`, real values filled in) | `/home/church-pi/Documents/.env` |
| `church-audio@.service` | `/etc/systemd/system/church-audio@.service` |
| `mount-church-music.service` | `/etc/systemd/system/mount-church-music.service` |
| `99-church-audio.rules` | `/etc/udev/rules.d/99-church-audio.rules` |

```bash
cp .env.example /home/church-pi/Documents/.env
nano /home/church-pi/Documents/.env     # fill in every setting; see README.md "Configuration"
chmod 600 /home/church-pi/Documents/.env

sudo cp church-audio@.service mount-church-music.service /etc/systemd/system/
sudo cp 99-church-audio.rules /etc/udev/rules.d/

sudo systemctl daemon-reload
sudo udevadm control --reload-rules
sudo udevadm trigger
```

The `.env` file is hidden because its name starts with a dot. Use `ls -la`, or Ctrl+H in the file manager.

If you are upgrading from an older version, add the new settings to your existing `.env` before running: `SFTP_HOST`, `SFTP_REMOTE_DIR` and `EMAIL_TO` are required, and the script exits with an error naming the missing one if they aren't set. Also set `ORG_NAME` and `FOLDER_PREFIX` (the old behavior used a "StJohns_" prefix) so your existing year folders and MP3 tags don't change.

You do not enable `church-audio@.service`. It is started on demand by the udev rule, once per inserted drive (for example `church-audio@sda1.service`).

## 4. Sudoers (one time)

The service mounts and unmounts the recording drive as the `church-pi` user, which needs passwordless `sudo` for exactly those two commands:

```bash
sudo visudo -f /etc/sudoers.d/church-audio
```

Contents:

```
church-pi ALL=(ALL) NOPASSWD: /bin/mount, /bin/umount
```

`visudo` checks the syntax when you save. This rule allows `mount` and `umount` with any arguments, which is acceptable for a dedicated appliance Pi but not for a shared computer.

## 5. Screen "sleep" — dim the backlight, don't power off the display

Originally this used `dpms_timeout` (full display power-off) in the labwc idle settings. Testing showed that afterward Raspberry Pi Connect screen sharing (WayVNC) could not recover and showed a grey screen until the Pi was rebooted, even though the touchscreen itself worked. Dimming the backlight instead avoids this, because the display is never actually off.

**Remove any existing DPMS timeout first.** Check `~/.config/wayfire.ini` and `~/.config/labwc/rc.xml` for a `dpms_timeout` line and delete it, so it doesn't conflict.

**Find your backlight device name** (it varies by touchscreen revision):

```bash
ls /sys/class/backlight/
cat /sys/class/backlight/<name>/max_brightness
```

Note the `<name>` (for example `10-0045`) and the maximum brightness (for example `31`).

**Make the brightness file writable without sudo:**

```bash
sudo nano /etc/udev/rules.d/98-backlight-permissions.rules
```

```
SUBSYSTEM=="backlight", RUN+="/bin/chmod 666 /sys/class/backlight/%k/brightness"
```

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
```

**Install swayidle** and add it to labwc's autostart:

```bash
sudo apt install -y swayidle
nano ~/.config/labwc/autostart
```

Add this line, replacing `<name>` and `31` with your values (`2` is dim but visible):

```bash
swayidle -w timeout 3600 'echo 2 > /sys/class/backlight/<name>/brightness' resume 'echo 31 > /sys/class/backlight/<name>/brightness' &
```

Reboot, then confirm that after an hour idle the screen dims (not black), touching it restores full brightness, and Pi Connect shows the desktop the whole time.

## 6. Test before trusting the automation

First confirm the output drive is mounted at `/mnt/church_music` (`lsblk -f`). Then plug in a drive that has a WAV file dated within the last 6 days (`YYYYMMDD*.wav`) and find its **partition** name:

```bash
lsblk -f        # e.g. sda1. It must NOT be LABEL=CHURCHMUSIC
```

Start the service by hand using that partition name:

```bash
sudo systemctl start church-audio@sda1.service
journalctl -u church-audio@sda1.service -n 50 --no-pager
```

Use the partition (`sda1`), not the whole disk (`sda`). Starting the whole disk gives "wrong fs type, bad option, bad superblock". If you edit a `.service` file, run `sudo systemctl daemon-reload` before testing again.

To watch live instead, use `journalctl -u church-audio@sda1.service -f` and press Ctrl+C to stop. That only closes the viewer, not the service.

When the run finishes, confirm:

- the MP3 landed in `/mnt/church_music/Music/<FOLDER_PREFIX><year>/`
- it was uploaded to the website
- a report email arrived
- no MP3 was written to the recording drive

Also plug in an unrelated USB drive with no matching WAV files. You should get a quick "No service files found" email, and nothing on that drive should change. That confirms the exclusion logic works.

Finally, unplug and replug the `CHURCHMUSIC` drive to check that `mount-church-music.service` remounts it.

## 7. Editing the script

Use VS Code's "Remote - SSH" extension from your own computer to edit `audio_exporter.py` on the Pi. Thonny (already on the Pi) works for quick edits over Pi Connect.

## 8. To run it manually from the terminal

```bash
/home/church-pi/church_audio_env/bin/python3 /home/church-pi/Documents/audio_exporter.py
```

## 9. Design decisions and known quirks

**systemd, not a direct udev command.** A `RUN+=` rule runs inside udev's worker, blocks other device events and is killed after a timeout. The udev rule only tags the device so systemd starts the service independently.

**Separate output drive.** Writing MP3s onto the Yamaha recording drive filled it and caused the recording buffer to skip. MP3s now go to the permanent `CHURCHMUSIC` drive.

**Whole-disk drives.** Some drives carry a filesystem with no partition table (`sda`, not `sda1`). The udev rule has one line for each case. The output drive must be partitioned, because its remount rule only matches `sd[a-z][0-9]`.

**Possible double mount.** The desktop automounter usually mounts the recording drive under `/media/church-pi/` as well, and the service mounts it again at `/mnt/church_usb`. Linux allows this and it works. The service unmounts only its own mount afterward.

**Drive deletion.** After a successful upload the script deletes all but the newest two WAV files on the recording drive, ordered by the date in the filename.

**Automatic eject.** When processing finishes, the recording drive is ejected automatically, so people can unplug it without the risk of a half-written filesystem. If an eject fails (for example, a file manager window still has the drive open), the failure is logged in the report email and the drive should be ejected by hand before it is removed.

**No automatic shutdown.** An earlier plan was to power the Pi off after each run and wake it Sunday morning. It was replaced by backlight dimming. If you want it later, the Pi 5 can wake from an RTC alarm: set `POWER_OFF_ON_HALT=1` in the bootloader (`sudo rpi-eeprom-config --edit`), write the wake time to `/sys/class/rtc/rtc0/wakealarm` and then power off. Test the full cycle first.

**Speed-up removed.** The 10% speed-up step was removed because it did not sound good. The code is kept as commented-out lines in `audio_exporter.py` (search for "speedup") in case you want it back.

**Not implemented.** The SFTP connection does not verify the host key.
