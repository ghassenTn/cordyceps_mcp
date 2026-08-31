const express = require('express');
const app = express();

app.use(express.json());
app.use(logger);

app.get('/health', (req, res) => res.json({ ok: true }));
app.post('/users', createUser);
app.get('/users/:id', getUserById);
app.delete('/users/:id', deleteUser);

function createUser(req, res) {
  res.send('created');
}

function getUserById(req, res) {
  res.send(req.params.id);
}

function deleteUser(req, res) {
  res.send('deleted');
}

function logger(req, res, next) {
  next();
}

module.exports = app;
