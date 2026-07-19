module.exports = {
  SERVER_PORT: 5417,
  get API_BASE() {
    return `http://127.0.0.1:${this.SERVER_PORT}`;
  },
};