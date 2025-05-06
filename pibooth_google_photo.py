# -*- coding: utf-8 -*-

"""Pibooth plugin to upload pictures on Google Photos."""

import os
import json
import os.path

import requests
try:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.oauth2.credentials import Credentials
except ImportError:
    InstalledAppFlow = None
    pass  # When running the setup.py, google-auth-oauthlib is not yet installed

import pibooth
from pibooth.utils import LOGGER


__version__ = "1.2.3"

SECTION = 'GOOGLE'
CACHE_FILE = '.google_token.json'


@pibooth.hookimpl
def pibooth_configure(cfg):
    """Declare the new configuration options"""
    cfg.add_option(SECTION, 'album_name', "Pibooth",
                   "Album where pictures are uploaded",
                   "Album name", "Pibooth")
    cfg.add_option(SECTION, 'client_id_file', '',
                   "Credentials file downloaded from Google API")
    cfg.add_option(SECTION, 'reduce_url_activated', False,
                   "Activate or deactivate URL reduction",
                   "Reduce URL Activated", False)
    cfg.add_option(SECTION, 'reduce_url', 'https://is.gd/create.php?format=json&url={url}',
                   "Service URL for reducing links",
                   "Reduce URL", 'https://is.gd/create.php?format=json&url={url}')


@pibooth.hookimpl
def pibooth_reset(cfg, hard):
    """Remove cached token file."""
    if hard and os.path.isfile(cfg.join_path(CACHE_FILE)):
        LOGGER.info("Remove Google Photos autorizations '%s'", cfg.join_path(CACHE_FILE))
        os.remove(cfg.join_path(CACHE_FILE))


@pibooth.hookimpl
def pibooth_startup(app, cfg):
    """Create the GooglePhotosUpload instance."""
    app.previous_picture_url = None
    client_id_file = cfg.getpath(SECTION, 'client_id_file')

    if not client_id_file:
        LOGGER.debug("No credentials file defined in [GOOGLE][client_id_file], upload deactivated")
    elif not os.path.exists(client_id_file):
        LOGGER.error("No such file [%s][client_id_file]='%s', please check config",
                     SECTION, client_id_file)
    elif client_id_file and os.path.getsize(client_id_file) == 0:
        LOGGER.error("Empty file [%s][client_id_file]='%s', please check config",
                     SECTION, client_id_file)
    else:
        LOGGER.info("Initialize Google Photos connection")
        app.google_photos = GooglePhotosApi(client_id_file, cfg.join_path(CACHE_FILE))


@pibooth.hookimpl(tryfirst=True)
def state_processing_exit(app, cfg):
    """Upload picture to google photo album and shorten URL if needed"""
    if hasattr(app, 'google_photos'):
        # Déterminer quel fichier uploader
        file_to_upload = app.previous_picture_file
        
        # Si on est en mode vidéo et qu'un GIF a été créé, utiliser le GIF
        if hasattr(app, 'selected_mode') and app.selected_mode == 'video' and hasattr(app, 'gif_path') and os.path.exists(app.gif_path):
            file_to_upload = app.gif_path
            LOGGER.info(f"Mode vidéo détecté, utilisation du GIF pour l'upload: {app.gif_path}")
        else:
            LOGGER.info(f"Mode photo ou pas de GIF, utilisation de l'image: {app.previous_picture_file}")
        
        photo_id = app.google_photos.upload(file_to_upload,
                                            cfg.get(SECTION, 'album_name'))

        if not photo_id:
            LOGGER.error("Échec critique de l'upload, annulation du processus")
            app.previous_picture_url = None
            return
        if photo_id is not None:
            app.previous_picture_url = app.google_photos.get_temp_url(photo_id)
            
            # Ajout de la logique de réduction d'URL
            if cfg.getboolean(SECTION, 'reduce_url_activated'):
                reduce_service = cfg.get(SECTION, 'reduce_url', fallback='').strip()
                if reduce_service:
                    try:
                        url = app.previous_picture_url
                        if not url.startswith('http'):
                            LOGGER.error("URL non valide pour le raccourcissement : %s", url)
                            return

                        api_url = reduce_service.format(url=url)
                        response = requests.get(api_url)

                        if response.status_code == 200:
                            shortened_url = response.json().get("shorturl")
                            if shortened_url:
                                app.previous_picture_url = shortened_url
                                LOGGER.debug(f"URL reduced: {shortened_url}")
                            else:
                                LOGGER.error("Invalid response from URL shortener")
                        else:
                            LOGGER.error(f"URL shortening error (HTTP {response.status_code}): {response.text}")
                    except Exception as e:
                        LOGGER.error(f"URL shortening failed: {str(e)}")
                else:
                    LOGGER.error("URL reduction activated but no service URL configured")
        else:
            app.previous_picture_url = None



class GooglePhotosApi(object):

    """Google Photos interface.

    A file with YOUR_CLIENT_ID and YOUR_CLIENT_SECRET is required, go to
    https://developers.google.com/photos/library/guides/get-started .

    A file ``token_file`` is generated at first run to store permanently the
    autorizations to use Google API.

    :param client_id: file generated from google API
    :type client_id: str
    :param token_file: file where generated token will be stored
    :type token_file: str
    """

    URL = 'https://photoslibrary.googleapis.com/v1'
    SCOPES = ['https://www.googleapis.com/auth/photoslibrary',
              'https://www.googleapis.com/auth/photoslibrary.sharing',
              'https://www.googleapis.com/auth/photoslibrary.appendonly']

    def __init__(self, client_id_file, token_file="token.json"):
        self.client_id_file = client_id_file
        self.token_cache_file = token_file

        self._albums_cache = {}  # Keep cache to avoid multiple request
        if self.is_reachable():
            self._session = self._get_authorized_session()
        else:
            self._session = None

    def _auth(self):
        """Open browser to create credentials."""
        flow = InstalledAppFlow.from_client_secrets_file(self.client_id_file, scopes=self.SCOPES)
        return flow.run_local_server(port=0)

    def _save_credentials(self, credentials):
        """Save credentials in a file to use API without need to allow acces."""
        try:
            with open(self.token_cache_file, 'w') as fp:
                fp.write(credentials.to_json())
        except OSError as err:
            LOGGER.warning("Can not save Google Photos token in file '%s': %s",
                           self.token_cache_file, err)

    def _get_authorized_session(self):
        """Create credentials file if required and open a new session."""
        credentials = None
        if not os.path.exists(self.token_cache_file) or \
                os.path.getsize(self.token_cache_file) == 0:
            LOGGER.debug("First use of plugin, store token in file '%s'",
                         self.token_cache_file)
            credentials = self._auth()
            self._save_credentials(credentials)
        else:
            credentials = Credentials.from_authorized_user_file(self.token_cache_file, self.SCOPES)
            with open(self.client_id_file) as fd:
                data = json.load(fd)
                if "web" in data:
                    data = data["web"]
                elif "installed" in data:
                    data = data["installed"]

            if credentials.client_id != data.get('client_id') or\
                    credentials.client_secret != data.get('client_secret'):
                LOGGER.debug("Application key or secret has changed, store new token in file '%s'",
                             self.token_cache_file)
                credentials = self._auth()
                self._save_credentials(credentials)
            elif credentials.expired:
                credentials.refresh(Request())
                self._save_credentials(credentials)


        if credentials:
            session = AuthorizedSession(credentials)
            session.headers.update({'Content-Type': 'application/json'})
            return session

        return None

    def is_reachable(self):
        """Check if Google Photos is reachable."""
        try:
            return requests.head('https://photos.google.com').status_code in (200, 302)
        except requests.ConnectionError:
            return False

    def get_albums(self, app_created_only=False):
        """Generator to loop through all Google Photos albums."""
        params = {
            'excludeNonAppCreatedData': app_created_only
        }
        while True:
            albums = self._session.get(self.URL + '/albums', params=params).json()
            LOGGER.debug("Google Photos server response: %s", albums)

            if 'albums' in albums:
                for album in albums["albums"]:
                    yield album
                if 'nextPageToken' in albums:
                    params["pageToken"] = albums["nextPageToken"]
                else:
                    return  # close generator
            else:
                return  # close generator

    def get_album_id(self, album_name):
        """Return the album ID if exists else None."""
        if album_name.lower() in self._albums_cache:
            return self._albums_cache[album_name.lower()]["id"]

        for album in self.get_albums(True):
            title = album["title"].lower()
            self._albums_cache[title] = album
            if title == album_name.lower():
                LOGGER.info("Found existing Google Photos album '%s'", album_name)
                return album["id"]
        return None

    def create_album(self, album_name):
        """Create a new album and return its ID."""
        LOGGER.info("Creating a new Google Photos album '%s'", album_name)
        create_album_body = json.dumps({"album": {"title": album_name}})

        resp = self._session.post(self.URL + '/albums', create_album_body).json()
        LOGGER.debug("Google Photos server response: %s", resp)

        if "id" in resp:
            return resp['id']

        LOGGER.error("Can not create Google Photos album '%s'", album_name)
        return None
        
    def upload(self, filename, album_name):
        """Upload a photo file to the given Google Photos album."""
        photo_id = None

        if not self.is_reachable():
            LOGGER.error("Google Photos upload failure: no internet connexion!")
            return photo_id

        if not self._session:
            self._session = self._get_authorized_session()

        album_id = self.get_album_id(album_name)
        if not album_id:
            album_id = self.create_album(album_name)
        if not album_id:
            LOGGER.error("Google Photos upload failure: album '%s' not found!", album_name)
            return photo_id

        # Étape 1: Upload du fichier pour obtenir le token
        upload_url = f'{self.URL}/uploads'
        headers = {
            'Content-Type': 'application/octet-stream',
            'X-Goog-Upload-Protocol': 'raw',
            'X-Goog-Upload-File-Name': os.path.basename(filename)
        }

        try:
            with open(filename, 'rb') as f:
                upload_resp = self._session.post(upload_url, headers=headers, data=f)
        except Exception as e:
            LOGGER.error("Upload failed: %s", str(e))
            return None

        if upload_resp.status_code != 200 or not upload_resp.content:
            LOGGER.error("Upload error (HTTP %s): %s", upload_resp.status_code, upload_resp.text)
            return None

        # Étape 2: Création du média dans l'album
        create_url = f'{self.URL}/mediaItems:batchCreate'
        create_body = {
            "albumId": album_id,
            "newMediaItems": [{
                "simpleMediaItem": {
                    "uploadToken": upload_resp.content.decode(),
                    "fileName": os.path.basename(filename)
                }
            }]
        }

        try:
            create_resp = self._session.post(create_url, json=create_body)
            create_resp.raise_for_status()
            response_data = create_resp.json()
        except Exception as e:
            LOGGER.error("Media creation failed: %s", str(e))
            return None

        if "newMediaItemResults" in response_data:
            result = response_data["newMediaItemResults"][0]
            status = result.get("status", {})
            
            # Google renvoie parfois "message" au lieu de "code" pour le succès
            if status.get("code") == 0 or "Success" in status.get("message", ""):
                photo_id = result['mediaItem']['id']
                LOGGER.info(f"Upload réussi : {filename} -> ID {photo_id}")
                LOGGER.debug("Réponse complète : %s", response_data)  # Debug supplémentaire
            else:
                LOGGER.error("Erreur d'upload : Code=%s | Message=%s", 
                            status.get("code"), 
                            status.get("message"))
        else:
            LOGGER.error("Invalid server response: %s", response_data)

        return photo_id


    def get_temp_url(self, photo_id):
        """
        Get the temporary URL for the picture (valid 1 hour only).
        """
        resp = self._session.get(self.URL + '/mediaItems/' + photo_id)
        if resp.status_code == 200:
            url = resp.json()['baseUrl']
            LOGGER.debug('Temporary picture URL -> %s', url)
            return url

        LOGGER.warning("Can not get temporary URL for Google Photos")
        return None
