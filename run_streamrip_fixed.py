async def run_streamrip(track_id: str, out_dir: Path) -> bool:
    global DEEZER_MAX_QUALITY
    arl = read_arl()
    if not arl:
        logger.warning("No ARL configured — skipping streamrip")
        return False

    # Get user's preferred quality from config
    user_quality = 1 # Default 320kbps
    if CONFIG_FILE.exists():
        content = read_config_raw()
        m = re.search(r'quality\s*=\s*"([^"]*)"', content)
        if m:
            q_str = m.group(1)
            if q_str == "FLAC": user_quality = 2
            elif q_str == "MP3_320": user_quality = 1
            elif q_str == "MP3_128": user_quality = 0

    # Start from the lower of (User Preference) vs (Last Known Max Capability)
    starting_quality = user_quality
    if DEEZER_MAX_QUALITY is not None:
        starting_quality = min(user_quality, DEEZER_MAX_QUALITY)

    # Qualities to try in descending order (2=FLAC, 1=320, 0=128)
    qualities_to_try = [q for q in [2, 1, 0] if q <= starting_quality]
    
    sr_config = CONFIG_DIR / "streamrip_config.toml"

    for q in qualities_to_try:
        add_log(f"Attempting Deezer download at quality level {q}...")
        
        try:
            cfg_text = STREAMRIP_CONFIG_TEMPLATE.replace("__ARL__", arl) 
                                              .replace("__FOLDER__", str(out_dir)) 
                                              .replace("__QUALITY__", str(q))
            sr_config.write_text(cfg_text)
        except Exception as e:
            logger.warning(f"Failed to write streamrip config: {e}")
            return False

        cmd = ["rip", "--config-path", str(sr_config), "url",
               f"https://www.deezer.com/track/{track_id}"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
            out = (stdout + stderr).decode(errors="replace")
            
            # Look for quality errors
            error_keywords = [
                "not available for your account",
                "does not support",
                "Codec not available",
                "not authorized",
                "not found" # Sometimes streamrip reports not found for forbidden quality
            ]
            
            if proc.returncode != 0:
                logger.warning(f"streamrip exited {proc.returncode} for quality {q}. Output: {out[:300]}")
                if any(k.lower() in out.lower() for k in error_keywords):
                    add_log(f"Quality {q} not supported by this account. Stepping down...", "WARNING")
                    continue
                else:
                    continue

            # Verify a file appeared
            files = list(out_dir.glob("*.*"))
            audio_exts = {".flac", ".mp3", ".m4a", ".ogg", ".opus"}
            if any(f.suffix.lower() in audio_exts for f in files):
                # SUCCESS! Remember this quality level
                if DEEZER_MAX_QUALITY is None or q > DEEZER_MAX_QUALITY:
                    # We only update if we haven't set it yet, or found a higher one (unlikely in step-down)
                    # but if we started at a lower preference, don't assume we can do higher
                    if DEEZER_MAX_QUALITY is None:
                        DEEZER_MAX_QUALITY = q
                        add_log(f"Account capability locked to quality level {q}")
                return True

        except FileNotFoundError:
            logger.error("The 'rip' command was not found in the system PATH.")
            add_log("Streamrip (rip) command not found. Using YouTube fallback.", "ERROR")
            return False
        except asyncio.TimeoutError:
            logger.warning(f"streamrip timed out at quality {q}")
            continue
        except Exception as e:
            logger.error(f"streamrip error at quality {q}: {e}")
            continue

    return False
