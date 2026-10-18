<img width="480" height="127" alt="logo" src="https://github.com/user-attachments/assets/bce59f9d-331a-444f-90f7-76bc63330e21" />

SnowCut is a desktop application for designing, scanning, and cutting with the CM100/CM110 scanning and cutting machine. It supplements the machine's built-in editing and cutting workflow with one that runs on a PC and communicates with the machine over the USB cable. It also allows you to scan and cut using an unrecognized/unsupported mat, or even no mat at all, as long as the media is wide enough to engage both roller grips.

I designed this software for myself because I was frustrated with the limitations imposed by the original firmware. I've tested it only as far as necessary to do what I use it for, and I've only tested it on my own machine. Please let me know via an issue if you have alignment issues or if there's anything else that needs improvement. The additional functions may allow you to do things with your machine that it is not designed to do, so please use with care! **This software is provided for free with no guarantee of suitability for any purpose. Use at your own risk.**

SnowCut requires the machine to be running the SnowCut custom firmware. If the machine is running the original firmware, SnowCut will offer to patch an original firmware file (.upf) so you can install it from a USB flash drive.

SnowCut is an independent project and is not affiliated with or endorsed by any equipment manufacturer.

If you use this software and it helps you get more out of your machine, please consider donating using Github Sponsorships or Ko-Fi.

## Requirements

- Windows 10 or 11
- If running from source, Python 3 with the packages in `requirements.txt` (not required if running the packaged release)
- [WinFsp](https://winfsp.dev/rel/), if you want to mount the machine's USB drive on your computer to access it as a removable drive.
- [Zadig](https://zadig.akeo.ie/), if you want to remove the UAC pop-up. Use it to install the libusb-win32 driver over the original device driver.

## Running SnowCut

From a release, extract the zip file and run SnowCut.exe.

## First connection

1. Connect the machine to the PC with a USB printer cable (B to A) and turn it on. The USB-B port is directly below the USB-A port on the side of the machine. There may be a removable flush cover hiding it.
2. Start SnowCut. When it starts, it will attempt to detect a CM110 connected to your computer.
3. If the machine is detected, SnowCut will ask to connect with administrator rights. Accept the prompt so it can communicate with the machine. You can also do this later from **Machine > Connect with Administrator Rights**.
4. Follow the prompt to choose an original `.upf` file. SnowCut will save a patched copy that you can copy to a USB flash drive and install on the machine using its normal update procedure. The firmware tools are also available in **Machine > Machine Settings**. Find the original firmware on the manufacturer's website. Supported firmware versions are `SNC00D0V1819W.upf` (Japan) and `SNC0004V1819W.upf` (North America). If your CM110 machine uses a different firmware region, post an issue here and I will attempt to add it to the list of compatible versions.

## Basic workflow

1. **Create or open a project.** Use **File > New Project** or **File > Open Project**. Projects are saved as `.sncproj` files.
2. **Add artwork.** Use **File > Import** to bring in SVG, PNG, JPEG, BMP, or PDF files, or draw directly on the mat with the shapes, pen, and text tools in the **Tools** panel.
3. **Choose the mat.** Open **Mat settings** from the **Device** panel and select the mat you are using. You can add the dimensions of a custom mat here or you can select an official 12x12 or 12x24 mat.
4. **Load the mat.** Press **Load / unload mat** in the **Device** panel.
5. **Scan (optional).** Press **Scan** to open the **Scan** panel. Press **Draw box** and drag a box on the canvas, or choose **Whole area**, choose where to save the image (and/or if you'd like to apply it directly to the canvas for alignment), and press **Scan**.
6. **Set color group settings** to select a tool, speed and pressure setting for each different line color used on the canvas. 
7. **Adjust the cut settings.** Open **Cut settings** from the **Device** panel to set the blade compensation settings. The Machine setting will give you results similar to the original firmware, while the default Custom settings should be slightly improved. You can also change the settings to match the dimensions of an aftermarket cutting blade that you have installed in your machine.
8. **Cut.** Press **Cut** in the **Device** panel, then press **Generate** to prepare the job and review the preview on the canvas. Press **Send to machine** to start cutting. Enable **Dry run (blade up)** first if you want to check the path without cutting. 

## Other features

- **Manual jog** lets you move the head to a position on the mat for testing.
- **Mount** lets you open the machine's USB drive as a removable drive (requires WinFsp).
- **Machine > Machine Settings** provides access to the machine's stored settings and firmware tools. Don't modify anything that you don't understand!

## User data

Mat profiles and other user data are stored in `Documents\SnowCut`.

## License

This software is available freely via the PolyForm Non-Commercial license. This is different from a normal "open source" license. It is a "source available" license. This is to prevent people from attempting to sell machines that have been modified with this software, etc. The art that you create using the software is yours. You can do whatever you want with it, including sell it. The license prevents the software itself from being repackaged or modified **and sold**. It's free to modify for personal use or to create your own freely available version.
