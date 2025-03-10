#!/usr/bin/env python3
"""
Asset Manager for DAKOSYS
Handles copying default assets (images, fonts) to the correct locations
"""

import os
import shutil
import logging
import yaml
import requests
from rich.console import Console
from shared_utils import setup_rotating_logger

# Define __all__ for explicit exports
__all__ = ['setup_assets', 'sync_anime_episode_collections', 'create_anime_overlay_files', 'update_anime_episode_collections']

# Initialize console for rich output
console = Console()

# Setup logger with rotation
if os.environ.get('RUNNING_IN_DOCKER') == 'true':
    data_dir = "/app/data"
else:
    data_dir = "data"  # Changed from DATA_DIR to a string literal

log_file = os.path.join(data_dir, "anime_trakt_manager.log")
logger = setup_rotating_logger("anime_trakt_manager", log_file)

# Container paths (where assets are stored in the Docker image)
CONTAINER_ASSETS_DIR = "/app/assets"
CONTAINER_FONTS_DIR = "/app/fonts"

def ensure_directory(directory):
    """Ensure a directory exists, creating it if necessary."""
    if not os.path.exists(directory):
        try:
            os.makedirs(directory, exist_ok=True)
            logger.info(f"Created directory: {directory}")
            return True
        except Exception as e:
            logger.error(f"Error creating directory {directory}: {str(e)}")
            return False
    return True

def get_kometa_paths(config):
    """Get overlay and collections paths from config, with fallbacks for backward compatibility."""
    # Check global kometa_config first (new format)
    yaml_output_dir = config.get('kometa_config', {}).get('yaml_output_dir')
    collections_dir = config.get('kometa_config', {}).get('collections_dir')

    # Fallback to tv_status_tracker config (old format)
    if not yaml_output_dir and 'services' in config and 'tv_status_tracker' in config['services']:
        yaml_output_dir = config['services']['tv_status_tracker'].get('yaml_output_dir')

    if not collections_dir and 'services' in config and 'tv_status_tracker' in config['services']:
        collections_dir = config['services']['tv_status_tracker'].get('collections_dir')

    # Final fallback to defaults
    if not yaml_output_dir:
        yaml_output_dir = '/kometa/config/overlays'

    if not collections_dir:
        collections_dir = '/kometa/config/collections'

    return yaml_output_dir, collections_dir

def copy_asset(source, destination):
    """Copy an asset file, creating destination directory if needed."""
    try:
        # Make sure destination directory exists
        dest_dir = os.path.dirname(destination)
        if not ensure_directory(dest_dir):
            return False

        # Copy the file
        shutil.copy2(source, destination)
        logger.info(f"Copied asset: {source} -> {destination}")
        return True
    except Exception as e:
        logger.error(f"Error copying asset {source} to {destination}: {str(e)}")
        return False

def setup_collection_posters(config):
    """Setup collection poster images."""
    # Get the Kometa paths with fallbacks
    _, collections_dir = get_kometa_paths(config)

    # Extract the parent directory
    kometa_config = os.path.dirname(collections_dir)

    # Ensure the assets directory exists
    assets_dir = os.path.join(kometa_config, "assets", "Next Airing")
    if not ensure_directory(assets_dir):
        return False

    # Define source and destination paths
    poster_source = os.path.join(CONTAINER_ASSETS_DIR, "next_airing_poster.jpg")
    poster_dest = os.path.join(assets_dir, "poster.jpg")

    # Copy the poster
    if os.path.exists(poster_source):
        return copy_asset(poster_source, poster_dest)
    else:
        logger.warning(f"Poster image not found in container: {poster_source}")
        return False

def setup_fonts(config):
    """Setup fonts for TV Status Tracker."""
    # Get font directory from config or use default
    kometa_config = "/kometa/config"
    if 'services' in config and 'tv_status_tracker' in config['services']:
        collections_dir = config['services']['tv_status_tracker'].get('collections_dir', '/kometa/config/collections')
        kometa_config = os.path.dirname(collections_dir)

    # Ensure the fonts directory exists
    fonts_dir = os.path.join(kometa_config, "fonts")
    if not ensure_directory(fonts_dir):
        return False

    # Copy the Juventus font
    font_source = os.path.join(CONTAINER_FONTS_DIR, "Juventus-Fans-Bold.ttf")
    font_dest = os.path.join(fonts_dir, "Juventus-Fans-Bold.ttf")

    if os.path.exists(font_source):
        if copy_asset(font_source, font_dest):
            # Update font path in config
            if 'services' in config and 'tv_status_tracker' in config['services']:
                config['services']['tv_status_tracker']['font_path'] = font_dest
                logger.info(f"Updated font path in config to: {font_dest}")
            return True
    else:
        logger.warning(f"Font not found in container: {font_source}")

    return False

def sync_anime_episode_collections(config, force_update=False):
    """Synchronize the anime episode type collections file with actual Trakt lists.

    Args:
        config: The application configuration
        force_update: Whether to force update even if no changes detected

    Returns:
        bool: True if update was successful, False otherwise
    """
    logger = logging.getLogger("asset_manager")

    # Get the Kometa paths
    yaml_output_dir, collections_dir = get_kometa_paths(config)

    # Ensure the collections directory exists
    if not ensure_directory(collections_dir):
        return False

    # Get Trakt username from config
    trakt_username = config.get('trakt', {}).get('username')
    if not trakt_username:
        logger.error("Trakt username not found in config - cannot proceed without it")
        return False

    # Get all lists from Trakt
    import trakt_auth
    access_token = trakt_auth.ensure_trakt_auth(quiet=True)
    if not access_token:
        logger.error("Failed to get Trakt access token")
        return False

    # Get all Trakt lists
    headers = trakt_auth.get_trakt_headers(access_token)
    trakt_api_url = 'https://api.trakt.tv'
    lists_url = f"{trakt_api_url}/users/{trakt_username}/lists"

    response = requests.get(lists_url, headers=headers)
    if response.status_code != 200:
        logger.error(f"Failed to get Trakt lists. Status: {response.status_code}")
        return False

    trakt_lists = response.json()

    # Organize lists by collection type
    collections_data = {
        'Fillers': [],
        'Manga Canon': [],
        'Anime Canon': [],
        'Mixed Canon/Filler': []
    }

    # Track which lists we found to detect changes
    found_lists = set()

    for trakt_list in trakt_lists:
        name = trakt_list.get('name', '')
        if '_' in name:
            parts = name.split('_', 1)
            if len(parts) == 2:
                anime_name, episode_type = parts

                # Format URL with slug for consistency
                list_slug = trakt_list.get('ids', {}).get('slug', name)
                list_url = f"https://trakt.tv/users/{trakt_username}/lists/{list_slug}"

                # Determine which collection this belongs to
                if episode_type.lower() == 'filler':
                    collections_data['Fillers'].append(list_url)
                    found_lists.add(list_url)
                elif episode_type.lower() == 'manga-canon':
                    collections_data['Manga Canon'].append(list_url)
                    found_lists.add(list_url)
                elif episode_type.lower() == 'manga canon':
                    collections_data['Manga Canon'].append(list_url)
                    found_lists.add(list_url)
                elif episode_type.lower() == 'anime-canon':
                    collections_data['Anime Canon'].append(list_url)
                    found_lists.add(list_url)
                elif episode_type.lower() == 'anime canon':
                    collections_data['Anime Canon'].append(list_url)
                    found_lists.add(list_url)
                elif episode_type.lower() == 'mixed-canon-filler':
                    collections_data['Mixed Canon/Filler'].append(list_url)
                    found_lists.add(list_url)
                elif episode_type.lower() == 'mixed canon/filler':
                    collections_data['Mixed Canon/Filler'].append(list_url)
                    found_lists.add(list_url)

    # Read existing collection file to preserve settings
    collections_file = os.path.join(collections_dir, 'anime_episode_type.yml')
    existing_collections = None
    changes_detected = force_update  # Start with force_update value

    if os.path.exists(collections_file):
        try:
            with open(collections_file, 'r') as file:
                existing_collections = yaml.safe_load(file) or {'collections': {}}
        except Exception as e:
            logger.error(f"Error reading existing collections file: {str(e)}")
            existing_collections = {'collections': {}}
    else:
        existing_collections = {'collections': {}}

    # Check if there are any changes compared to existing file
    if not force_update and existing_collections:
        for collection_name, collection_data in existing_collections.get('collections', {}).items():
            existing_lists = set(collection_data.get('trakt_list', []))
            if collection_name in collections_data:
                new_lists = set(collections_data[collection_name])
                if existing_lists != new_lists:
                    logger.info(f"Changes detected in {collection_name} collection")
                    changes_detected = True
                    break

    # Only update if changes detected or force_update
    if changes_detected:
        # Create the new collections structure
        new_collections = {'collections': {}}

        # For each collection type, preserve settings and update the lists
        for collection_name, list_urls in collections_data.items():
            # Get existing settings or use defaults
            collection_settings = {}
            if existing_collections and 'collections' in existing_collections and collection_name in existing_collections['collections']:
                collection_settings = existing_collections['collections'][collection_name].copy()
                # Update only the trakt_list field
                collection_settings['trakt_list'] = list_urls
            else:
                # Use default settings
                collection_settings = {
                    'trakt_list': list_urls,
                    'sync_mode': 'sync',
                    'item_label': collection_name.replace(' Canon/Filler', '').replace(' Canon', 'Canon'),
                    'builder_level': 'episode',
                    'cache_builders': 6
                }

            new_collections['collections'][collection_name] = collection_settings

        # Write the collections file
        try:
            with open(collections_file, 'w') as file:
                yaml.dump(new_collections, file, default_flow_style=False, sort_keys=False)
            
            # Also create/update the overlay files
            create_anime_overlay_files(config)
            
            return True
        except Exception as e:
            logger.error(f"Error writing collections file: {str(e)}")
            return False
    else:
        logger.info("No changes detected in anime episode collections")
        return True

def create_anime_overlay_files(config):
    """Create the overlay files for anime episode types."""
    # Get the Kometa paths with fallbacks
    yaml_output_dir, _ = get_kometa_paths(config)
    logger = logging.getLogger("asset_manager")
    overlay_settings = config.get('services', {}).get('anime_episode_type', {}).get('overlay', {})

    # Ensure the overlays directory exists
    if not ensure_directory(yaml_output_dir):
        return False

    font_path = "config/fonts/Juventus-Fans-Bold.ttf"

    # Create the four overlay files with your custom overlay_names
    overlay_configs = {
        'fillers.yml': {
            'overlay_name': 'filler_overlay',
            'name': 'Filler',
            'label': 'Filler'
        },
        'manga_canon.yml': {
            'overlay_name': 'manga_overlay',
            'name': 'Manga Canon',
            'label': 'MangaCanon'
        },
        'anime_canon.yml': {
            'overlay_name': 'anime_overlay',
            'name': 'Anime Canon',
            'label': 'AnimeCanon'
        },
        'mixed.yml': {
            'overlay_name': 'mixed_overlay',
            'name': 'Mixed Canon/Filler',
            'label': 'Mixed'
        }
    }

    success = True
    for filename, values in overlay_configs.items():
        overlay_file = os.path.join(yaml_output_dir, filename)
        
        # Skip if file already exists
        if os.path.exists(overlay_file):
            continue
            
        overlay_content = {
            'overlays': {
                values['overlay_name']: {  
                    'builder_level': 'episode',
                    'overlay': {
                        'name': f"text({values['name']})",
                        'horizontal_offset': overlay_settings.get('horizontal_offset', 0),
                        'horizontal_align': overlay_settings.get('horizontal_align', 'center'),
                        'vertical_offset': overlay_settings.get('vertical_offset', 0),
                        'vertical_align': overlay_settings.get('vertical_align', 'top'),
                        'font_size': overlay_settings.get('font_size', 75),
                        'font': font_path,
                        'back_width': overlay_settings.get('back_width', 1920),
                        'back_height': overlay_settings.get('back_height', 125),
                        'back_color': overlay_settings.get('back_color', '#262626')
                    },
                    'plex_search': {
                        'all': {
                            'episode_label': values['label']
                        }
                    }
                }
            }
        }

        try:
            with open(overlay_file, 'w') as file:
                yaml.dump(overlay_content, file, default_flow_style=False, sort_keys=False)
        except Exception as e:
            logger.error(f"Error creating overlay file {filename}: {str(e)}")
            success = False

    return success

def update_anime_episode_collections(config):
    """Update the anime episode type collections file in Kometa.
    Now uses the sync_anime_episode_collections function to ensure YAML stays in sync with Trakt lists."""
    # Use the sync function which handles all cases: new lists, deleted lists, and setting preservation
    return sync_anime_episode_collections(config, force_update=True)

def setup_assets(config):
    """Setup all assets for DAKOSYS."""
    console.print("[bold blue]Setting up DAKOSYS assets...[/bold blue]")

    # Setup collection posters
    poster_result = setup_collection_posters(config)
    if poster_result:
        console.print("[green]Collection poster setup successfully[/green]")
    else:
        console.print("[yellow]Collection poster setup failed or skipped[/yellow]")

    # Setup fonts
    font_result = setup_fonts(config)
    if font_result:
        console.print("[green]Fonts setup successfully[/green]")
    else:
        console.print("[yellow]Fonts setup failed or skipped[/yellow]")

    # Setup anime episode collections
    if config.get('services', {}).get('anime_episode_type', {}).get('enabled', False):
        console.print("[blue]Setting up anime episode type collections...[/blue]")
        collections_result = update_anime_episode_collections(config)
        if collections_result:
            console.print("[green]Anime episode collections setup successfully[/green]")
        else:
            console.print("[yellow]Anime episode collections setup failed[/yellow]")

        console.print("[blue]Setting up anime episode type overlays...[/blue]")
        overlays_result = create_anime_overlay_files(config)
        if overlays_result:
            console.print("[green]Anime episode overlays setup successfully[/green]")
        else:
            console.print("[yellow]Anime episode overlays setup failed[/yellow]")

    return True

# Can be run directly to set up assets
if __name__ == "__main__":
    import yaml

    # Load config
    config_path = "/app/config/config.yaml" if os.environ.get('RUNNING_IN_DOCKER') == 'true' else "config/config.yaml"
    try:
        with open(config_path, 'r') as file:
            config = yaml.safe_load(file)

        # Save config with any updates from asset setup
        setup_result = setup_assets(config)

        if setup_result:
            # Save the updated config
            with open(config_path, 'w') as file:
                yaml.dump(config, file)
    except Exception as e:
        logger.error(f"Error loading configuration: {str(e)}")
        console.print(f"[red]Error: {str(e)}[/red]")
